import re
import warnings
from collections import defaultdict

import freud
import gsd
import gsd.hoomd
import mbuild as mb
import numpy as np
from openbabel import pybel


def get_bonded(compound, particle):
    """
    Returns a list of particles bonded to the given particle
    in a compound

    Parameters
    ----------
    compound : mbuild.Compound, compound containing particle
    particle : mbuild.Particle, particle to which we want to
               find the bonded neighbors.

    Returns
    -------
    list of mbuild.Particles
    """
    def is_particle(i,j):
        if i is particle:
            return j
        elif j is particle:
            return i
        else:
            return False
    return [is_particle(i,j) for i,j in compound.bonds() if is_particle(i,j)]


def get_index(compound, particle):
    """
    Get the index of a particle in the compound so that the particle
    can be accessed like compound[index]

    Parameters
    ----------
    compound: mbuild.Compound, compound which contains particle
    particle: mbuild.Particle, particle for which to fetch the index

    Returns
    -------
    int
    """
    return [p for p in compound.particles()].index(particle)


def remove_hydrogen(compound, particle):
    """
    Remove a hydrogen attached to particle. Particle name must be "H".
    If no hydrogen is bonded to particle, do nothing.

    Parameters
    ----------
    compound: mbuild.Compound, compound which contains particle
    particle: mbuild.Particle, particle from which to remove a hydrogen
    """
    hydrogens = [i for i in get_bonded(compound, particle) if i.name == "H"]
    if hydrogens:
        compound.remove(hydrogens[0])


def backmap(cg_compound, bead_dict, bond_dict):
    """
    Creates a fine-grained compound from a coarse one given dictionaries
    specifying the bead and how to place bonds.

    Parameters
    ----------
    cg_compound: mbuild.Compound, coarse grained compound
    bead_dict: dictionary of dictionaries, specifies what SMILES string
               and bond anchors to use for each bead type
               For example:
                 bead_dict = {
                 "_B": {
                     "smiles": "c1sccc1",
                     "anchors": [0,2,4],
                     "posres": 1
                     },
                 }
               specifies that coarse grain bead "_B" should be replaced
               with the fine-grain structure represented by the SMILES string
               "c1sccc1", should form bonds to other fine-grained beads
               from atoms 0, 2, and 4, and should have a position restraint
               attached to atom 1 (optional).
    bond_dict: dictionary of list of tuples, specifies what fine-grain bond
               should replace the bonds in the coarse structure.
               For example:
                bond_dict = {
                    "_B_B": [(0,2),(2,0)],
                }
               specifies that the bond between two "_B" beads should happen
               in their fine-grain replacement between the 0th and 2nd or
               the 2nd and 0th atoms

    Returns
    -------
    mbuild.Compound
    """
    fine_grained = mb.Compound()

    anchors = dict()
    for i,bead in enumerate(cg_compound.particles()):
        smiles = bead_dict[bead.name]["smiles"]
        b = mb.load(smiles, smiles=True)
        b.translate_to(bead.pos)
        anchors[i] = dict()
        for index in bead_dict[bead.name]["anchors"]:
            anchors[i][index] = b[index]
        try:
            posres_ind = bead_dict[bead.name]["posres"]
            posres = mb.Particle(name="X", pos=bead.pos)
            b.add(posres)
            b.add_bond((posres,b[posres_ind]))
        except KeyError:
            pass
        fine_grained.add(b)

    bonded_atoms = []
    for ibead,jbead in cg_compound.bonds():
        i = get_index(cg_compound, ibead)
        j = get_index(cg_compound, jbead)
        names = [ibead.name,jbead.name]
        bondname = "".join(names)
        try:
            bonds = bond_dict[bondname]
        except KeyError:
            try:
                bondname = "".join(names[::-1])
                bonds = [(j,i) for (i,j) in bond_dict[bondname]]
            except KeyError:
                print(f"{bondname} not defined in bond dictionary.")
                raise
        # choose a starting distance that is way too big
        mindist = max(cg_compound.boundingbox.lengths)
        for fi,fj in bonds:
            iatom = anchors[i][fi]
            jatom = anchors[j][fj]
            if (iatom in bonded_atoms) or (jatom in bonded_atoms):
                # assume only one bond from the CG translates
                # to the FG structure
                continue
            dist = distance(iatom.pos,jatom.pos)
            if dist < mindist:
                fi_best = fi
                fj_best = fj
                mindist = dist
        iatom = anchors[i][fi_best]
        jatom = anchors[j][fj_best]
        fine_grained.add_bond((iatom, jatom))

        bonded_atoms.append(iatom)
        bonded_atoms.append(jatom)

    for atom in bonded_atoms:
        remove_hydrogen(fine_grained,atom)

    return fine_grained


def bin_distribution(vals, nbins, start=None, stop=None):
    """
    Calculates a distribution given an array of data

    Parameters
    ----------
    vals : np.ndarry (N,), values over which to calculate the distribution
    start : float, value to start bins (default min(bonds_dict[bond]))
    stop : float, value to stop bins (default max(bonds_dict[bond]))
    step : float, step size between bins (default (stop-start)/30)

    Returns
    -------
    np.ndarray (nbins,2), where the first column is the mean value of the bin and
    the second column is number of values which fell into that bin
    """
    if start == None:
        start = min(vals)
    if stop == None:
        stop = max(vals)
    step = (stop - start) / nbins

    bins = [i for i in np.arange(start, stop, step)]
    dist = np.empty([len(bins) - 1, 2])
    for i, length in enumerate(bins[1:]):
        in_bin = [b for b in vals if b > bins[i] and b < bins[i + 1]]
        dist[i, 1] = len(in_bin)
        dist[i, 0] = np.mean((bins[i], bins[i + 1]))
    return dist


def autocorr1D(array):
    """
    Takes in a linear numpy array, performs autocorrelation
    function and returns normalized array with half the length
    of the input
    """
    ft = np.fft.rfft(array - np.average(array))
    acorr = np.fft.irfft(ft * np.conjugate(ft)) / (len(array) * np.var(array))
    return acorr[0 : len(acorr) // 2]


def get_decorr(acorr):
    """
    Returns the decorrelation time of the autocorrelation, a 1D numpy array
    """
    return np.argmin(acorr > 0)


def error_analysis(data):
    """
    Returns the standard and relative error given a dataset in a 1D numpy array
    """
    serr = np.std(data) / np.sqrt(len(data))
    rel_err = np.abs(100 * serr / np.average(data))
    return (serr, rel_err)


def get_angle(a, b, c):
    """
    Calculates the angle between three points a-b-c

    Parameters
    ----------
    a,b,c : np.ndarrays, positions of points a, b, and c

    Returns
    -------
    float, angle in radians
    """
    ba = a - b
    bc = c - b

    cos = np.dot(ba, bc) / (np.linalg.norm(ba) * np.linalg.norm(bc))
    return np.arccos(cos)


def get_molecules(snapshot):
    """
    Creates list of sets of connected atom indices

    This code adapted from Matias Thayer's:
    https://stackoverflow.com/questions/10301000/python-connected-components

    Parameters
    ----------
    snapshot : gsd.hoomd.Snapshot

    Returns
    -------
    list of sets of connected atom indices
    """

    def _snap_bond_graph(snapshot):
        """
        Given a snapshot from a trajectory create a graph of the bonds

        get_molecules(gsd.hoomd.Snapshot) --> dict of sets
        """
        bond_array = snapshot.bonds.group
        bond_graph = defaultdict(set)
        for row in bond_array:
            bond_graph[row[0]].add(row[1])
            bond_graph[row[1]].add(row[0])
        return bond_graph

    def _get_connected_group(node, already_seen):
        """
        This code adapted from Matias Thayer's:
        https://stackoverflow.com/questions/10301000/python-connected-components
        """

        result = set()
        nodes = set([node])
        while nodes:
            node = nodes.pop()
            already_seen.add(node)
            nodes.update(graph[node] - already_seen)
            result.add(node)
        return result, already_seen

    graph = _snap_bond_graph(snapshot)

    already_seen = set()
    result = []
    for node in graph:
        if node not in already_seen:
            connected_group, already_seen = _get_connected_group(node, already_seen)
            result.append(connected_group)
    return result


def map_good_on_bad(good_mol, bad_mol):
    """
    This function takes a correctly-typed (good) and a poorly-typed (bad)
    pybel molecule and transfers the bond and atom typing from the good to
    the bad molecule but retains the atom positions.
    It assumes that both molecules have the same number of particles and
    they maintain their order.
    Changes:
    atom- type, isaromatic
    bond- order, isaromatic

    Parameters
    ----------
    good_mol, bad_mol : pybel.Molecule

    Returns
    -------
    pybel.Molecule
    """

    for i in range(1, good_mol.OBMol.NumAtoms()):
        good_atom = good_mol.OBMol.GetAtom(i)
        bad_atom = bad_mol.OBMol.GetAtom(i)
        bad_atom.SetType(good_atom.GetType())
        bad_atom.SetAromatic(good_atom.IsAromatic())

    for i in range(1, good_mol.OBMol.NumBonds()):
        good_bond = good_mol.OBMol.GetBond(i)
        bad_bond = bad_mol.OBMol.GetBond(i)
        bad_bond.SetBondOrder(good_bond.GetBondOrder())
        bad_bond.SetAromatic(good_bond.IsAromatic())

    return bad_mol


def save_mol_to_file(good_mol, filename):
    """
    This function takes a correctly-typed (good) pybel molecule and saves
    the bond and atom typing to a file for later use.

    Parameters
    ----------
    good_mol : pybel.Molecule
    filename : str, name of file

    use map_file_on_bad() to use this file
    """

    with open(filename, "w") as f:
        for i in range(1, good_mol.OBMol.NumAtoms()):
            good_atom = good_mol.OBMol.GetAtom(i)
            f.write(f"{good_atom.GetType()}   {good_atom.IsAromatic()}\n")

        for i in range(1, good_mol.OBMol.NumBonds()):
            good_bond = good_mol.OBMol.GetBond(i)
            f.write(f"{good_bond.GetBO()}   {good_bond.IsAromatic()}\n")


def map_file_on_bad(filename, bad_mol):
    """
    This function takes a filename containing correctly-typed and a poorly-typed (bad)
    pybel molecule and transfers the bond and atom typing from the good to
    the bad molecule but retains the atom positions.
    It assumes that both molecules have the same number of particles and
    they maintain their order.
    Changes:
    atom- type, isaromatic
    bond- order, isaromatic

    Parameters
    ----------
    filename : str, generated using save_mol_to_file()
    bad_mol : pybel.Molecule

    Returns
    -------
    pybel.Molecule
    """
    with open(filename, "r") as f:
        lines = f.readlines()
    atoms = []
    bonds = []
    for line in lines:
        one, two = line.split()
        if not one.isdigit():
            atoms.append((one, two))
        else:
            bonds.append((one, two))

    for i in range(1, bad_mol.OBMol.NumAtoms()):
        bad_atom = bad_mol.OBMol.GetAtom(i)
        bad_atom.SetType(atoms[i - 1][0])
        if atoms[i - 1][1] == "True":
            bad_atom.SetAromatic()
        else:
            bad_atom.UnsetAromatic()

    for i in range(1, bad_mol.OBMol.NumBonds()):
        bad_bond = bad_mol.OBMol.GetBond(i)
        bad_bond.SetBO(int(bonds[i - 1][0]))
        if bonds[i - 1][1] == "True":
            bad_bond.SetAromatic()
        else:
            bad_bond.UnsetAromatic()

    return bad_mol


def has_number(string):
    """
    Returns True if string contains a number.
    Else returns False.
    """
    return bool(re.search("[0-9]", string))


def has_common_member(set_a, tup):
    """
    return True if set_a (set) and tup (tuple) share a common member
    else return False
    """
    set_b = set(tup)
    return set_a & set_b



def num2str(num):
    """
    Returns a capital letter for positive integers up to 701
    e.g. num2str(0) = 'A'
    """
    if num < 26:
        return chr(num + 65)
    return "".join([chr(num // 26 + 64), chr(num % 26 + 65)])


# features SMARTS
features_dict = {
    "thiophene": "c1sccc1",
    "thiophene_F": "c1scc(F)c1",
    "alkyl_3": "CCC",
    "benzene": "c1ccccc1",
    "splitring1": "csc",
    "splitring2": "cc",
    "twobenzene": "c2ccc1ccccc1c2",
    "ring_F": "c1sc2c(scc2c1F)",
    "ring_3": "c3sc4cc5ccsc5cc4c3",
    "chain1": "OCC(CC)CCCC",
    "chain2": "CCCCC(CC)COC(=O)",
    "cyclopentadiene": "C1cccc1",
    "c4": "cC(c)(c)c",
    "cyclopentadienone": "C=C1C(=C)ccC1=O",
}
