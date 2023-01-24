"""
This module will be used to construct the surfaces and interfaces used in this package.
"""
from OgreInterface.surfaces import Surface, Interface
from OgreInterface import utils
from OgreInterface.lattice_match import ZurMcGill

from pymatgen.core.structure import Structure
from pymatgen.analysis.structure_matcher import StructureMatcher
from pymatgen.core.lattice import Lattice
from pymatgen.io.vasp.inputs import Poscar
from pymatgen.io.ase import AseAtomsAdaptor
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
from pymatgen.analysis.interfaces.zsl import ZSLGenerator
from pymatgen.core.operations import SymmOp

from tqdm import tqdm
import numpy as np
import math
from copy import deepcopy
from typing import Union, List
from itertools import combinations, product, groupby
from ase import Atoms
from multiprocessing import Pool, cpu_count
import time

from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform


class TolarenceError(RuntimeError):
    pass


class SurfaceGenerator:
    """
    The SurfaceGenerator classes generates surfaces with all possible terminations and contains
    information about the Miller indices of the surface and the number of different
    terminations.

    Parameters:
        structure (pymatgen.core.structure.Structure or ase.Atoms): Conventional bulk structure.
        miller_index (list): Miller index of the created surfaces.
        layers (int): Number of layers generated in the surface.
        vacuum (float): Size of vacuum in Angstroms.
    """

    def __init__(
        self,
        bulk: Union[Structure, Atoms],
        miller_index: List[int],
        layers: int,
        vacuum: float,
        convert_to_conventional: bool = True,
        generate_all: bool = True,
        filter_ionic_slabs: bool = False,
        lazy: bool = False,
    ):
        self.convert_to_conventional = convert_to_conventional

        (
            self.bulk_structure,
            self.bulk_atoms,
            self.primitive_structure,
            self.primitive_atoms,
        ) = self._get_bulk(atoms_or_struc=bulk)

        self.use_prim = len(self.bulk_structure) != len(
            self.primitive_structure
        )

        self.point_group_operations = self._get_point_group_operations()

        self.miller_index = miller_index
        self.layers = layers
        self.vacuum = vacuum
        self.generate_all = generate_all
        self.filter_ionic_slabs = filter_ionic_slabs
        self.lazy = lazy
        (
            self.oriented_bulk_structure,
            self.oriented_bulk_atoms,
            self.uvw_basis,
            self.transformation_matrix,
            self.inplane_vectors,
            self.surface_normal,
            self.surface_normal_projection,
        ) = self._get_oriented_bulk_structure()

        if not self.lazy:
            self.slabs = self._generate_slabs()

    def generate_slabs(self):
        if self.lazy:
            self.slabs = self._generate_slabs()
        else:
            print(
                "The slabs are already generated upon initialization. This function is only needed if lazy=True"
            )

    @classmethod
    def from_file(
        cls,
        filename,
        miller_index: List[int],
        layers: int,
        vacuum: float,
        convert_to_conventional: bool = True,
        generate_all: bool = True,
        filter_ionic_slabs: bool = False,
        lazy: bool = False,
    ):
        structure = Structure.from_file(filename=filename)

        return cls(
            structure,
            miller_index,
            layers,
            vacuum,
            convert_to_conventional,
            generate_all,
            filter_ionic_slabs,
            lazy,
        )

    # @property
    # def primitive_transformation_matrix(self):
    #     return self._transformation_matrix

    # @property
    # def conventional_transformation_matrix(self):
    #     return self.uvw_basis

    def _get_bulk(self, atoms_or_struc):
        if type(atoms_or_struc) == Atoms:
            init_structure = AseAtomsAdaptor.get_structure(atoms_or_struc)
        elif type(atoms_or_struc) == Structure:
            init_structure = atoms_or_struc
        else:
            raise TypeError(
                f"structure accepts 'pymatgen.core.structure.Structure' or 'ase.Atoms' not '{type(atoms_or_struc).__name__}'"
            )

        sg = SpacegroupAnalyzer(init_structure)
        prim_structure = sg.get_primitive_standard_structure()
        prim_atoms = AseAtomsAdaptor.get_atoms(prim_structure)

        if self.convert_to_conventional:
            conventional_structure = sg.get_conventional_standard_structure()
            conventional_atoms = AseAtomsAdaptor.get_atoms(
                conventional_structure
            )

            return (
                conventional_structure,
                conventional_atoms,
                prim_structure,
                prim_atoms,
            )
        else:
            init_atoms = AseAtomsAdaptor().get_atoms(init_structure)

            return init_structure, init_atoms, prim_structure, prim_atoms

    def _get_point_group_operations(self):
        sg = SpacegroupAnalyzer(self.bulk_structure)
        point_group_operations = sg.get_point_group_operations(cartesian=False)
        operation_array = np.array(
            [p.rotation_matrix for p in point_group_operations]
        ).astype(np.int8)
        unique_operations = np.unique(operation_array, axis=0)

        return unique_operations

    def _get_oriented_bulk_structure(self):
        bulk = self.bulk_structure
        prim_bulk = self.primitive_structure

        lattice = bulk.lattice
        prim_lattice = prim_bulk.lattice

        recip_lattice = lattice.reciprocal_lattice_crystallographic
        prim_recip_lattice = prim_lattice.reciprocal_lattice_crystallographic

        miller_index = self.miller_index

        d_hkl = lattice.d_hkl(miller_index)
        normal_vector = recip_lattice.get_cartesian_coords(miller_index)
        prim_miller_index = prim_recip_lattice.get_fractional_coords(
            normal_vector
        )
        prim_miller_index = utils._get_reduced_vector(
            prim_miller_index
        ).astype(int)

        normal_vector /= np.linalg.norm(normal_vector)

        sg = SpacegroupAnalyzer(bulk)
        prim_mapping = sg.get_symmetry_dataset()["mapping_to_primitive"]
        _, prim_inds = np.unique(prim_mapping, return_index=True)

        bulk.add_site_property(
            "bulk_wyckoff", sg.get_symmetry_dataset()["wyckoffs"]
        )
        bulk.add_site_property(
            "bulk_equivalent",
            sg.get_symmetry_dataset()["equivalent_atoms"].tolist(),
        )
        prim_bulk.add_site_property(
            "bulk_wyckoff",
            [sg.get_symmetry_dataset()["wyckoffs"][i] for i in prim_inds],
        )
        prim_bulk.add_site_property(
            "bulk_equivalent",
            sg.get_symmetry_dataset()["equivalent_atoms"][prim_inds].tolist(),
        )

        if not self.use_prim:
            intercepts = np.array(
                [1 / i if i != 0 else 0 for i in miller_index]
            )
            non_zero_points = np.where(intercepts != 0)[0]
            lattice_for_slab = lattice
            struc_for_slab = bulk
        else:
            intercepts = np.array(
                [1 / i if i != 0 else 0 for i in prim_miller_index]
            )
            non_zero_points = np.where(intercepts != 0)[0]
            d_hkl = lattice.d_hkl(miller_index)
            lattice_for_slab = prim_lattice
            struc_for_slab = prim_bulk

        if len(non_zero_points) == 1:
            basis = np.eye(3)
            dot_products = basis.dot(normal_vector)
            sort_inds = np.argsort(dot_products)
            basis = basis[sort_inds]

            if np.linalg.det(basis) < 0:
                basis = basis[[1, 0, 2]]

            basis = basis

        if len(non_zero_points) == 2:
            points = intercepts * np.eye(3)
            vec1 = points[non_zero_points[1]] - points[non_zero_points[0]]
            vec2 = np.eye(3)[intercepts == 0]

            basis = np.vstack([vec1, vec2])

        if len(non_zero_points) == 3:
            points = intercepts * np.eye(3)
            possible_vecs = []
            for center_inds in [[0, 1, 2], [1, 0, 2], [2, 0, 1]]:
                vec1 = (
                    points[non_zero_points[center_inds[1]]]
                    - points[non_zero_points[center_inds[0]]]
                )
                vec2 = (
                    points[non_zero_points[center_inds[2]]]
                    - points[non_zero_points[center_inds[0]]]
                )
                cart_vec1 = lattice_for_slab.get_cartesian_coords(vec1)
                cart_vec2 = lattice_for_slab.get_cartesian_coords(vec2)
                angle = np.arccos(
                    np.dot(cart_vec1, cart_vec2)
                    / (np.linalg.norm(cart_vec1) * np.linalg.norm(cart_vec2))
                )
                possible_vecs.append((vec1, vec2, angle))

            chosen_vec1, chosen_vec2, angle = min(
                possible_vecs, key=lambda x: abs(x[-1])
            )

            basis = np.vstack([chosen_vec1, chosen_vec2])

        basis = utils.get_reduced_basis(basis)

        if len(basis) == 2:
            max_normal_search = 2

            index_range = sorted(
                reversed(range(-max_normal_search, max_normal_search + 1)),
                key=lambda x: abs(x),
            )
            candidates = []
            for uvw in product(index_range, index_range, index_range):
                if (not any(uvw)) or abs(
                    np.linalg.det(np.vstack([basis, uvw]))
                ) < 1e-8:
                    continue

                vec = lattice_for_slab.get_cartesian_coords(uvw)
                proj = np.abs(np.dot(vec, normal_vector) - d_hkl)
                vec_length = np.linalg.norm(vec)
                cosine = np.dot(vec / vec_length, normal_vector)
                candidates.append((uvw, cosine, vec_length, proj))
                if abs(abs(cosine) - 1) < 1e-8:
                    # If cosine of 1 is found, no need to search further.
                    break
            # We want the indices with the maximum absolute cosine,
            # but smallest possible length.
            uvw, cosine, l, diff = max(
                candidates, key=lambda x: (-x[3], x[1], -x[2])
            )
            basis = np.vstack([basis, uvw])

        init_oriented_struc = struc_for_slab.copy()
        init_oriented_struc.make_supercell(basis)

        cart_basis = init_oriented_struc.lattice.matrix

        if np.linalg.det(cart_basis) < 0:
            ab_switch = np.array([[0, 1, 0], [1, 0, 0], [0, 0, 1]])
            init_oriented_struc.make_supercell(ab_switch)
            basis = ab_switch.dot(basis)
            cart_basis = init_oriented_struc.lattice.matrix

        cross_ab = np.cross(cart_basis[0], cart_basis[1])
        cross_ab /= np.linalg.norm(cross_ab)
        cross_ac = np.cross(cart_basis[0], cross_ab)
        cross_ac /= np.linalg.norm(cross_ac)

        ortho_basis = np.vstack(
            [
                cart_basis[0] / np.linalg.norm(cart_basis[0]),
                cross_ac,
                cross_ab,
            ]
        )

        to_planar_operation = SymmOp.from_rotation_and_translation(
            ortho_basis, translation_vec=np.zeros(3)
        )

        planar_oriented_struc = init_oriented_struc.copy()
        planar_oriented_struc.apply_operation(to_planar_operation)

        planar_matrix = deepcopy(planar_oriented_struc.lattice.matrix)

        new_a, new_b, mat = utils.reduce_vectors_zur_and_mcgill(
            planar_matrix[0, :2], planar_matrix[1, :2]
        )

        planar_oriented_struc.make_supercell(mat)

        a_norm = (
            planar_oriented_struc.lattice.matrix[0]
            / planar_oriented_struc.lattice.a
        )
        a_to_i = np.array(
            [[a_norm[0], -a_norm[1], 0], [a_norm[1], a_norm[0], 0], [0, 0, 1]]
        )

        a_to_i_operation = SymmOp.from_rotation_and_translation(
            a_to_i.T, translation_vec=np.zeros(3)
        )
        planar_oriented_struc.apply_operation(a_to_i_operation)
        planar_oriented_struc.sort()

        planar_oriented_atoms = AseAtomsAdaptor().get_atoms(
            planar_oriented_struc
        )

        final_matrix = deepcopy(planar_oriented_struc.lattice.matrix)

        final_basis = mat.dot(basis)
        final_basis = utils.get_reduced_basis(final_basis).astype(int)

        transformation_matrix = np.copy(final_basis)

        if self.use_prim:
            for i, b in enumerate(final_basis):
                cart_coords = prim_lattice.get_cartesian_coords(b)
                conv_frac_coords = lattice.get_fractional_coords(cart_coords)
                conv_frac_coords = utils._get_reduced_vector(conv_frac_coords)
                final_basis[i] = conv_frac_coords

        inplane_vectors = final_matrix[:2]

        norm = np.cross(final_matrix[0], final_matrix[1])
        norm /= np.linalg.norm(norm)

        if np.dot(norm, final_matrix[-1]) < 0:
            norm *= -1

        norm_proj = np.dot(norm, final_matrix[-1])

        return (
            planar_oriented_struc,
            planar_oriented_atoms,
            final_basis,
            transformation_matrix,
            inplane_vectors,
            norm,
            norm_proj,
        )

    def _calculate_possible_shifts(self, tol: float = 0.1):
        frac_coords = self.oriented_bulk_structure.frac_coords
        n = len(frac_coords)

        if n == 1:
            # Clustering does not work when there is only one data point.
            shift = frac_coords[0][2] + 0.5
            return [shift - math.floor(shift)]

        # We cluster the sites according to the c coordinates. But we need to
        # take into account PBC. Let's compute a fractional c-coordinate
        # distance matrix that accounts for PBC.
        dist_matrix = np.zeros((n, n))
        # h = self.oriented_bulk_structure.lattice.matrix[-1, -1]
        h = self.surface_normal_projection
        # Projection of c lattice vector in
        # direction of surface normal.
        for i, j in combinations(list(range(n)), 2):
            if i != j:
                cdist = frac_coords[i][2] - frac_coords[j][2]
                cdist = abs(cdist - round(cdist)) * h
                dist_matrix[i, j] = cdist
                dist_matrix[j, i] = cdist

        condensed_m = squareform(dist_matrix)
        z = linkage(condensed_m)
        clusters = fcluster(z, tol, criterion="distance")

        # Generate dict of cluster# to c val - doesn't matter what the c is.
        c_loc = {c: frac_coords[i][2] for i, c in enumerate(clusters)}

        # Put all c into the unit cell.
        possible_c = [c - math.floor(c) for c in sorted(c_loc.values())]

        # Calculate the shifts
        nshifts = len(possible_c)
        shifts = []
        for i in range(nshifts):
            if i == nshifts - 1:
                # There is an additional shift between the first and last c
                # coordinate. But this needs special handling because of PBC.
                shift = (possible_c[0] + 1 + possible_c[i]) * 0.5
                if shift > 1:
                    shift -= 1
            else:
                shift = (possible_c[i] + possible_c[i + 1]) * 0.5
            shifts.append(shift - math.floor(shift))

        shifts = sorted(shifts)

        return shifts

    def get_slab(self, shift=0, tol: float = 0.1, energy=None):
        """
        This method takes in shift value for the c lattice direction and
        generates a slab based on the given shift. You should rarely use this
        method. Instead, it is used by other generation algorithms to obtain
        all slabs.

        Arg:
            shift (float): A shift value in Angstrom that determines how much a
                slab should be shifted.
            tol (float): Tolerance to determine primitive cell.
            energy (float): An energy to assign to the slab.

        Returns:
            (Slab) A Slab object with a particular shifted oriented unit cell.
        """
        init_matrix = deepcopy(self.oriented_bulk_structure.lattice.matrix)
        slab_base = self.oriented_bulk_structure.copy()
        slab_base.translate_sites(
            indices=range(len(slab_base)),
            vector=[0, 0, -shift],
            frac_coords=True,
            to_unit_cell=True,
        )

        z_coords = slab_base.frac_coords[:, -1]
        bot_z = z_coords.min()
        top_z = z_coords.max()
        bottom_layer_dist = np.abs(bot_z - (top_z - 1)) * init_matrix[-1, -1]
        top_layer_dist = np.abs((bot_z + 1) - top_z) * init_matrix[-1, -1]

        slab_base.make_supercell([1, 1, self.layers])
        slab_base.sort()

        vacuum_scale = self.vacuum // self.surface_normal_projection

        if vacuum_scale % 2:
            vacuum_scale += 1

        if vacuum_scale == 0:
            vacuum_scale = 1

        vacuum_transform = np.eye(3)
        vacuum_transform[-1, -1] = self.layers + vacuum_scale
        vacuum_matrix = vacuum_transform @ init_matrix

        # c_zero_inds = np.where(
        #     np.isclose(
        #         slab_base.frac_coords[:, -1],
        #         slab_base.frac_coords[:, -1].min(),
        #     )
        # )[0]
        # print(slab_base.frac_coords[c_zero_inds])

        non_orthogonal_slab = Structure(
            lattice=Lattice(matrix=vacuum_matrix),
            species=slab_base.species,
            coords=slab_base.cart_coords,
            coords_are_cartesian=True,
            to_unit_cell=True,
            site_properties=slab_base.site_properties,
        )
        non_orthogonal_slab.sort()
        non_orthogonal_min_atom = non_orthogonal_slab.frac_coords[
            np.argmin(non_orthogonal_slab.frac_coords[:, -1])
        ]
        non_orthogonal_slab.translate_sites(
            indices=range(len(non_orthogonal_slab)),
            vector=-non_orthogonal_min_atom,
            frac_coords=True,
            to_unit_cell=True,
        )

        a, b, c = non_orthogonal_slab.lattice.matrix
        new_c = np.dot(c, self.surface_normal) * self.surface_normal

        orthogonal_matrix = np.vstack([a, b, new_c])
        orthogonal_slab = Structure(
            lattice=Lattice(matrix=orthogonal_matrix),
            species=non_orthogonal_slab.species,
            coords=non_orthogonal_slab.cart_coords,
            coords_are_cartesian=True,
            to_unit_cell=True,
            site_properties=non_orthogonal_slab.site_properties,
        )
        orthogonal_slab.sort()

        shift = 0.5 * (vacuum_scale / (vacuum_scale + self.layers))
        non_orthogonal_slab.translate_sites(
            indices=range(len(non_orthogonal_slab)),
            vector=[0, 0, shift],
            frac_coords=True,
            to_unit_cell=True,
        )
        orthogonal_slab.translate_sites(
            indices=range(len(orthogonal_slab)),
            vector=[0, 0, shift],
            frac_coords=True,
            to_unit_cell=True,
        )

        return (
            orthogonal_slab,
            non_orthogonal_slab,
            bottom_layer_dist,
            top_layer_dist,
        )

    # def _get_ewald_energy(self, slab):
    #     slab = deepcopy(slab)
    #     bulk = deepcopy(self.pmg_structure)
    #     slab.add_oxidation_state_by_guess()
    #     bulk.add_oxidation_state_by_guess()
    #     E_slab = EwaldSummation(slab).total_energy
    #     E_bulk = EwaldSummation(bulk).total_energy
    #     return E_slab, E_bulk

    def _generate_slabs(self):
        """
        This function is used to generate slab structures with all unique
        surface terminations.

        Returns:
            A list of Surface classes
        """
        # Determine if all possible terminations are generated
        possible_shifts = self._calculate_possible_shifts()
        orthogonal_slabs = []
        non_orthogonal_slabs = []
        bottom_layer_dists = []
        top_layer_dists = []
        if not self.generate_all:
            (
                orthogonal_slab,
                non_orthogonal_slab,
                bottom_layer_dist,
                top_layer_dist,
            ) = self.get_slab(shift=possible_shifts[0])
            orthogonal_slab.sort_index = 0
            non_orthogonal_slab.sort_index = 0
            orthogonal_slabs.append(orthogonal_slab)
            non_orthogonal_slabs.append(non_orthogonal_slab)
            bottom_layer_dists.append(bottom_layer_dist)
            top_layer_dists.append(top_layer_dist)
        else:
            for i, possible_shift in enumerate(possible_shifts):
                (
                    orthogonal_slab,
                    non_orthogonal_slab,
                    bottom_layer_dist,
                    top_layer_dist,
                ) = self.get_slab(shift=possible_shift)
                orthogonal_slab.sort_index = i
                non_orthogonal_slab.sort_index = i
                orthogonal_slabs.append(orthogonal_slab)
                non_orthogonal_slabs.append(non_orthogonal_slab)
                bottom_layer_dists.append(bottom_layer_dist)
                top_layer_dists.append(top_layer_dist)

        surfaces = []

        if self.use_prim:
            base_structure = self.primitive_structure
        else:
            base_structure = self.bulk_structure

        # Loop through slabs to ensure that they are all properly oriented and reduced
        # Return Surface objects
        for i, slab in enumerate(orthogonal_slabs):
            # Create the Surface object
            surface = Surface(
                orthogonal_slab=slab,
                non_orthogonal_slab=non_orthogonal_slabs[i],
                primitive_oriented_bulk=self.oriented_bulk_structure,
                conventional_bulk=self.bulk_structure,
                base_structure=base_structure,
                transformation_matrix=self.transformation_matrix,
                miller_index=self.miller_index,
                layers=self.layers,
                vacuum=self.vacuum,
                uvw_basis=self.uvw_basis,
                point_group_operations=self.point_group_operations,
                bottom_layer_dist=bottom_layer_dists[i],
                top_layer_dist=top_layer_dists[i],
            )
            surfaces.append(surface)

        return surfaces

    def __len__(self):
        return len(self.slabs)

    @property
    def nslabs(self):
        """
        Return the number of slabs generated by the SurfaceGenerator
        """
        return self.__len__()

    @property
    def terminations(self):
        """
        Return the terminations of each slab generated by the SurfaceGenerator
        """
        return {i: slab.get_termination() for i, slab in enumerate(self.slabs)}


class InterfaceGenerator:
    """
    This class will use the lattice matching algorithm from Zur and McGill to generate
    commensurate interface structures between two inorganic crystalline materials.
    """

    def __init__(
        self,
        substrate: Surface,
        film: Surface,
        area_tol: float = 0.01,
        angle_tol: float = 0.01,
        length_tol: float = 0.01,
        max_area: float = 500.0,
        interfacial_distance: Union[float, None] = 2.0,
        sub_strain_frac: float = 0.0,
        vacuum: float = 40.0,
        center: bool = False,
    ):
        if type(substrate) == Surface:
            self.substrate = substrate
        else:
            raise TypeError(
                f"InterfaceGenerator accepts 'ogre.core.Surface' not '{type(substrate).__name__}'"
            )

        if type(film) == Surface:
            self.film = film
        else:
            raise TypeError(
                f"InterfaceGenerator accepts 'ogre.core.Surface' not '{type(film).__name__}'"
            )

        self.center = center
        self.area_tol = area_tol
        self.angle_tol = angle_tol
        self.length_tol = length_tol
        self.max_area = max_area
        self.interfacial_distance = interfacial_distance
        self.sub_strain_frac = sub_strain_frac
        self.vacuum = vacuum
        self.match_list = self._generate_interface_props()

    def _generate_interface_props(self):
        zm = ZurMcGill(
            film_vectors=self.film.inplane_vectors,
            substrate_vectors=self.substrate.inplane_vectors,
            film_basis=self.film.uvw_basis,
            substrate_basis=self.substrate.uvw_basis,
            max_area=self.max_area,
            max_linear_strain=self.length_tol,
            max_angle_strain=self.angle_tol,
            max_area_mismatch=self.area_tol,
        )
        match_list = zm.run(return_all=True)

        if len(match_list) == 0:
            raise TolarenceError(
                "No interfaces were found, please increase the tolarences."
            )
        elif len(match_list) == 1:
            return match_list
        else:
            film_basis_vectors = []
            sub_basis_vectors = []
            film_scale_factors = []
            sub_scale_factors = []
            for i, match in enumerate(match_list):
                film_basis_vectors.append(match.film_sl_basis)
                sub_basis_vectors.append(match.substrate_sl_basis)
                film_scale_factors.append(match.film_sl_scale_factors)
                sub_scale_factors.append(match.substrate_sl_scale_factors)

            film_basis_vectors = np.vstack(film_basis_vectors).astype(np.int8)
            sub_basis_vectors = np.vstack(sub_basis_vectors).astype(np.int8)
            film_scale_factors = np.concatenate(film_scale_factors).astype(
                np.int8
            )
            sub_scale_factors = np.concatenate(sub_scale_factors).astype(
                np.int8
            )

            film_map = self._get_miller_index_map(
                self.film.point_group_operations, film_basis_vectors
            )
            sub_map = self._get_miller_index_map(
                self.substrate.point_group_operations, sub_basis_vectors
            )

            split_film_basis_vectors = np.vsplit(
                film_basis_vectors, len(match_list)
            )
            split_sub_basis_vectors = np.vsplit(
                sub_basis_vectors, len(match_list)
            )
            split_film_scale_factors = np.split(
                film_scale_factors, len(match_list)
            )
            split_sub_scale_factors = np.split(
                sub_scale_factors, len(match_list)
            )

            sort_vecs = []

            for i in range(len(split_film_basis_vectors)):
                fb = split_film_basis_vectors[i]
                sb = split_sub_basis_vectors[i]
                fs = split_film_scale_factors[i]
                ss = split_sub_scale_factors[i]
                sort_vec = np.concatenate(
                    [
                        [ss[0]],
                        sub_map[tuple(sb[0])],
                        [ss[1]],
                        sub_map[tuple(sb[1])],
                        [fs[0]],
                        film_map[tuple(fb[0])],
                        [fs[1]],
                        film_map[tuple(fb[1])],
                    ]
                )
                sort_vecs.append(sort_vec)

            sort_vecs = np.vstack(sort_vecs)
            unique_sort_vecs, unique_sort_inds = np.unique(
                sort_vecs, axis=0, return_index=True
            )
            unique_matches = [match_list[i] for i in unique_sort_inds]

            sorted_matches = sorted(
                unique_matches,
                key=lambda x: (x.area, max(x.linear_strain), x.angle_strain),
            )

            return sorted_matches

    def _get_miller_index_map(self, operations, miller_indices):
        miller_indices = np.unique(miller_indices, axis=0)
        not_used = np.ones(miller_indices.shape[0]).astype(bool)
        op = np.einsum("...ij,jk", operations, miller_indices.T)
        op = op.transpose(2, 0, 1)
        unique_vecs = {}

        for i, vec in enumerate(miller_indices):
            if not_used[i]:
                same_inds = (op == vec).all(axis=2).sum(axis=1) > 0

                if not_used[same_inds].all():
                    same_vecs = miller_indices[same_inds]
                    optimal_vec = self._get_optimal_miller_index(same_vecs)
                    unique_vecs[tuple(optimal_vec)] = list(
                        map(tuple, same_vecs)
                    )
                    not_used[same_inds] = False

        mapping = {}
        for key, value in unique_vecs.items():
            for v in value:
                mapping[v] = key

        return mapping

    def _get_optimal_miller_index(self, vecs):
        diff = np.abs(np.sum(np.sign(vecs), axis=1))
        like_signs = vecs[diff == np.max(diff)]
        if len(like_signs) == 1:
            return like_signs[0]
        else:
            first_max = like_signs[
                np.abs(like_signs)[:, 0] == np.max(np.abs(like_signs)[:, 0])
            ]
            if len(first_max) == 1:
                return first_max[0]
            else:
                second_max = first_max[
                    np.abs(first_max)[:, 1] == np.max(np.abs(first_max)[:, 1])
                ]
                if len(second_max) == 1:
                    return second_max[0]
                else:
                    return second_max[
                        np.argmax(np.sign(second_max).sum(axis=1))
                    ]

    def _build_interface(self, match):
        if self.interfacial_distance is None:
            i_dist = (
                self.substrate.top_layer_dist + self.film.bottom_layer_dist
            ) / 2
        else:
            i_dist = self.interfacial_distance

        interface = Interface(
            substrate=self.substrate,
            film=self.film,
            interfacial_distance=i_dist,
            match=match,
            vacuum=self.vacuum,
            center=self.center,
        )
        return interface

    def generate_interfaces(self):
        if self.interfacial_distance is None:
            i_dist = (
                self.substrate.top_layer_dist + self.film.bottom_layer_dist
            ) / 2
        else:
            i_dist = self.interfacial_distance

        interfaces = []

        print("Generating Interfaces:")
        for match in tqdm(self.match_list, dynamic_ncols=True):
            interface = Interface(
                substrate=self.substrate,
                film=self.film,
                interfacial_distance=i_dist,
                match=match,
                vacuum=self.vacuum,
                center=self.center,
            )
            interfaces.append(interface)

        return interfaces
