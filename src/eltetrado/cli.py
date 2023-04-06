import argparse
import gzip
import logging
import os
import sys
import tempfile
from typing import IO, List, Optional

import orjson
import rnapolis.annotator
import rnapolis.parser
from rnapolis.annotator import LeontisWesthof, Structure2D
from rnapolis.common import BasePair, Residue, Stacking
from rnapolis.tertiary import Structure3D

from eltetrado.analysis import Visualizer, eltetrado, has_tetrad
from eltetrado.dto import generate_dto


def eltetrado_cli():
    with open(os.path.join(os.path.dirname(__file__), "VERSION")) as f:
        version = f.read().strip()

    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", help="path to input PDB or PDBx/mmCIF file")
    parser.add_argument("-o", "--output", help="(optional) path for output JSON file")
    parser.add_argument(
        "-m", "--model", help="(optional) model number to process", default=1, type=int
    )
    parser.add_argument(
        "--stacking-mismatch",
        help="a perfect tetrad stacking covers 4 nucleotides; this option can be used with value 1 or "
        "2 to allow this number of nucleotides to be non-stacked with otherwise well aligned "
        "tetrad [default=2]",
        default=2,
        type=int,
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="nucleotides in tetrad are found when linked only by cWH pairing",
    )
    parser.add_argument(
        "--no-reorder",
        action="store_true",
        help="chains of bi- and tetramolecular quadruplexes should be reordered to be able to have "
        "them classified; when this is set, chains will be processed in original order, which for "
        "bi-/tetramolecular means that they will likely be misclassified; use with care!",
    )
    parser.add_argument(
        "--complete-2d",
        action="store_true",
        help="when set, the visualization will also show canonical base pairs to provide context for "
        "the quadruplex",
    )
    parser.add_argument(
        "--no-image",
        action="store_true",
        help="when set, the visualization will not be created at all",
    )
    parser.add_argument(
        "--dssr-json",
        help="(optional) provide a JSON file generated by DSSR to read the secondary structure information from (use --nmr and --json switches)",
    )
    parser.add_argument(
        "-v", "--version", action="version", version="%(prog)s {}".format(version)
    )
    args = parser.parse_args()

    if not args.input:
        print(parser.print_help())
        sys.exit(1)

    cif_or_pdb = handle_input_file(args.input)
    structure3d = rnapolis.parser.read_3d_structure(
        cif_or_pdb, args.model, nucleic_acid_only=False
    )
    structure2d = (
        rnapolis.annotator.extract_secondary_structure(structure3d, args.model)
        if args.dssr_json is None
        else read_secondary_structure_from_dssr(structure3d, args.model, args.dssr_json)
    )

    analysis = eltetrado(
        structure2d, structure3d, args.strict, args.no_reorder, args.stacking_mismatch
    )
    print(analysis)

    if not args.no_image:
        visualizer = Visualizer(
            analysis, analysis.tetrads, args.complete_2d, analysis.global_index
        )

        basename = os.path.basename(args.input)
        root, ext = os.path.splitext(basename)
        if ext == ".gz":
            root, ext = os.path.splitext(root)
        prefix = root
        suffix = "str"
        visualizer.visualize(prefix, suffix)

        for i, helix in enumerate(analysis.helices):
            hv = Visualizer(
                analysis, helix.tetrads, args.complete_2d, analysis.global_index
            )
            suffix = "h{}".format(i + 1)
            hv.visualize(prefix, suffix)

            for j, quadruplex in enumerate(helix.quadruplexes):
                qv = Visualizer(
                    analysis,
                    quadruplex.tetrads,
                    args.complete_2d,
                    analysis.global_index,
                )
                qv.visualize(prefix, "{}-q{}".format(suffix, j + 1))

                for k, tetrad in enumerate(quadruplex.tetrads):
                    tv = Visualizer(
                        analysis, [tetrad], args.complete_2d, analysis.global_index
                    )
                    tv.visualize(prefix, "{}-q{}-t{}".format(suffix, j + 1, k + 1))

    if args.output:
        dto = generate_dto(analysis)

        with open(args.output, "wb") as jsonfile:
            jsonfile.write(orjson.dumps(dto))


def has_tetrad_cli():
    with open(os.path.join(os.path.dirname(__file__), "VERSION")) as f:
        version = f.read().strip()

    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", help="path to input PDB or PDBx/mmCIF file")
    parser.add_argument(
        "-m", "--model", help="(optional) model number to process", default=1, type=int
    )
    parser.add_argument(
        "--dssr-json",
        help="(optional) provide a JSON file generated by DSSR to read the secondary structure information from (use --nmr and --json switches)",
    )
    parser.add_argument(
        "--version", action="version", version="%(prog)s {}".format(version)
    )
    args = parser.parse_args()

    if not args.input:
        print(parser.print_help())
        sys.exit(1)

    cif_or_pdb = handle_input_file(args.input)
    structure3d = rnapolis.parser.read_3d_structure(
        cif_or_pdb, args.model, nucleic_acid_only=False
    )
    structure2d = (
        rnapolis.annotator.extract_secondary_structure(structure3d, args.model)
        if args.dssr_json is None
        else read_secondary_structure_from_dssr(structure3d, args.model, args.dssr_json)
    )
    flag = has_tetrad(structure2d, structure3d)
    sys.exit(0 if flag else 1)


def handle_input_file(path) -> IO[str]:
    root, ext = os.path.splitext(path)

    if ext == ".gz":
        root, ext = os.path.splitext(root)
        file = tempfile.NamedTemporaryFile("w+", suffix=ext)
        with gzip.open(path, "rt") as f:
            file.write(f.read())
            file.seek(0)
    else:
        file = tempfile.NamedTemporaryFile("w+", suffix=ext)
        with open(path) as f:
            file.write(f.read())
            file.seek(0)
    return file


def read_secondary_structure_from_dssr(
    structure3d: Structure3D, model: int, dssr_json_path: str
) -> Structure2D:
    base_pairs: List[BasePair] = []
    stackings: List[Stacking] = []

    with open(dssr_json_path) as f:
        dssr = orjson.loads(f.read())

    for result in dssr.get("models", []):
        if result.get("model", None) == model:
            dssr = result.get("parameters", {})
            break

    for pair in dssr.get("pairs", []):
        nt1 = match_dssr_name_to_residue(structure3d, pair.get("nt1", None))
        nt2 = match_dssr_name_to_residue(structure3d, pair.get("nt2", None))
        lw = match_dssr_lw(pair.get("LW", None))

        if nt1 is not None and nt2 is not None and lw is not None:
            base_pairs.append(BasePair(nt1, nt2, lw, None))

    for stack in dssr.get("stacks", []):
        nts = [
            match_dssr_name_to_residue(structure3d, nt)
            for nt in stack.get("nts_long", "").split(",")
        ]
        for i in range(1, len(nts)):
            nt1 = nts[i - 1]
            nt2 = nts[i]
            if nt1 is not None and nt2 is not None:
                stackings.append(Stacking(nt1, nt2, None))

    return Structure2D(base_pairs, stackings, [], [], [], "", "", "", [], [], [], [])


def match_dssr_name_to_residue(
    structure3d: Structure3D, nt_id: Optional[str]
) -> Optional[Residue]:
    if nt_id is not None:
        nt_id = nt_id.split(":")[-1]
        for residue in structure3d.residues:
            if residue.full_name == nt_id:
                return residue
        logging.warn(f"Failed to find residue {nt_id}")
    return None


def match_dssr_lw(lw: Optional[str]) -> Optional[LeontisWesthof]:
    return LeontisWesthof[lw] if lw in dir(LeontisWesthof) else None


if __name__ == "__main__":
    eltetrado_cli()
