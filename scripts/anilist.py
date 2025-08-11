#!/usr/bin/env python3

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from enum import Enum
from pathlib import Path
from typing import NoReturn


class Namespace(str, Enum):
    SVG = "http://www.w3.org/2000/svg"
    XHTML = "http://www.w3.org/1999/xhtml"
    XLINK = "http://www.w3.org/1999/xlink"

    def __str__(self):
        return super().__str__()


class Tag(str, Enum):
    DIV = f"{{{Namespace.XHTML}}}div"
    IMG = f"{{{Namespace.XHTML}}}img"
    SECTION = f"{{{Namespace.XHTML}}}section"
    IMAGE = f"{{{Namespace.SVG}}}image"

    def __str__(self):
        return super().__str__()


NSMAP = {
    "svg": Namespace.SVG,
    "xhtml": Namespace.XHTML,
    "xlink": Namespace.XLINK,
}

ET.register_namespace("", Namespace.SVG)  # default ns for <svg>
ET.register_namespace("xhtml", Namespace.XHTML)  # XHTML inside foreignObject
ET.register_namespace("xlink", Namespace.XLINK)  # legacy href


class AniListSVG:
    @staticmethod
    def err(msg: str, code: int = 1) -> NoReturn:
        print(f"[error] {msg}", file=sys.stderr)
        sys.exit(code)

    @staticmethod
    def warn(msg: str) -> None:
        print(f"[warn] {msg}", file=sys.stderr)

    @staticmethod
    def info(msg: str) -> None:
        print(f"[info] {msg}", file=sys.stdout)

    @staticmethod
    def parse_svg(path: Path) -> ET.ElementTree:
        if not path.is_file():
            AniListSVG.err(f"SVG not found: {path}")
        try:
            return ET.parse(str(path))
        except ET.ParseError as e:
            AniListSVG.err(f"Failed to parse SVG '{path}': {e}")

    def extract_uris_data(self, root: ET.Element, max_images: int | None) -> list[str]:
        """Extract base64 `data:image/*` URIs from an AniList-style source SVG.

        Search order (first match wins):
            - Strict: `.anilist` > `.characters` > `<img src="data:...">`
            - Relaxed: any `<img src="data:...">` under `.anilist`
            - Fallback: `<svg:image>` `@href` / `@xlink:href` with data URIs

        Non-data URIs are skipped with a warning. Respects `max_images` and
        preserves document order.
        """

        def is_data_uri(uri: str | None) -> bool:
            pattern = re.compile(r"^data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+$")
            return bool(pattern.match(uri.strip())) if uri else False

        # 1) Strict: .anilist > .characters > img[src]
        candidates = root.findall(
            ".//xhtml:div[@class='anilist']/xhtml:div[@class='characters']/xhtml:img",
            NSMAP,
        )

        # 2) Relaxed: any img[src] under .anilist
        if not candidates:
            candidates = root.findall(
                ".//xhtml:div[@class='anilist']//xhtml:img", NSMAP
            )

        data_uris: list[str] = []

        if candidates:
            for element in candidates:
                uri = element.get("src")

                if is_data_uri(uri):
                    data_uris.append(uri.strip())

                elif uri:
                    self.warn(
                        f"Non-data URI ignored (expected base64 data:image/*) {uri[:60]}…"
                    )

                if max_images and len(data_uris) >= max_images:
                    return data_uris

        # 3) Fallback: any <svg:image> @href / @xlink:href with data URIs
        if not data_uris:
            svg_images = root.findall(".//svg:image", NSMAP)

            for element in svg_images:
                uri = element.get(f"{{{Namespace.XLINK}}}href") or element.get("href")

                if is_data_uri(uri):
                    data_uris.append(uri.strip())

                elif uri:
                    self.warn(f"Non-data URI in <image> ignored: {uri[:60]}…")

                if max_images and len(data_uris) >= max_images:
                    break

        if not data_uris:
            self.err(
                "No base64 data URIs found in source SVG. "
                'Expected <img src="data:image/...;base64,..."> under .anilist/.characters, '
                'or <image href="data:image/...">.'
            )

        return data_uris

    def find_object(self, root: ET.Element, merge: bool) -> ET.Element:
        """
        Select and prepare a `<foreignObject>` in an SVG for injection.

        Note: When `merge` is False, this function MUTATES the selected `<foreignObject>` by removing all child nodes.
        """

        foreign_object = list(root.findall(".//svg:foreignObject", NSMAP))

        if not foreign_object:
            self.err("No <foreignObject> elements found in target SVG.")

        def has_anilist(element: ET.Element) -> bool:
            return element.find(".//xhtml:div[@class='anilist']", NSMAP) is not None

        anilist_fos = [obj for obj in foreign_object if has_anilist(obj)]
        other_fos = [obj for obj in foreign_object if not has_anilist(obj)]

        if merge:
            return anilist_fos[0] if anilist_fos else foreign_object[0]

        # 1) Prefer a foreignObject without .anilist to avoid accidental merge
        target = other_fos[0] if other_fos else foreign_object[0]

        # 2) Override: clear all existing children so we fully replace content
        for child in target:
            target.remove(child)

        return target

    def build_anilist_section(self, images: list[str], width: int, height: int):
        metrics = ET.Element(Tag.DIV, {"id": "metrics-end"})

        items = ET.Element(Tag.DIV, {"class": "items-wrapper"})
        outer_section = ET.SubElement(items, Tag.SECTION)

        row = ET.SubElement(outer_section, Tag.DIV, {"class": "row fill-width"})
        inner_section = ET.SubElement(row, Tag.SECTION)

        anilist = ET.SubElement(inner_section, Tag.DIV, {"class": "anilist"})
        characters = ET.SubElement(anilist, Tag.DIV, {"class": "characters"})

        for uri in images:
            ET.SubElement(
                characters,
                Tag.IMG,
                {
                    "src": uri,
                    "width": str(width),
                    "height": str(height),
                    "alt": "character",
                },
            )

        return [items, metrics]

    def inject(
        self,
        target_svg: Path,
        source_svg: Path,
        output: Path | None,
        max_images: int | None,
        width: int,
        height: int,
        merge: bool,
    ) -> None:
        source_file = self.parse_svg(path=source_svg)
        data_uris = self.extract_uris_data(source_file.getroot(), max_images)
        self.info(f"Pulled {len(data_uris)} image(s) from source")

        target_file = self.parse_svg(path=target_svg)
        fo = self.find_object(target_file.getroot(), merge)

        # Force full size
        fo.set("x", "0")
        fo.set("y", "0")
        fo.set("width", "100%")
        fo.set("height", "100%")

        for child in self.build_anilist_section(data_uris, width, height):
            fo.append(child)

        try:
            path = output or target_svg.with_suffix(".out.svg")
            target_file.write(path, encoding="utf-8", xml_declaration=True)
            self.info(f"Done --> {path}")
        except Exception as e:
            self.err(f"Failed to write output SVG: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Replace a target SVG <foreignObject> with a fixed XHTML structure populated with <img> data URIs extracted from a source SVG."
    )

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(2)

    parser.add_argument("svg", type=Path, help="Path to the target SVG to modify")
    parser.add_argument(
        "-s",
        "--source",
        type=Path,
        required=True,
        help="Path to the source SVG containing the <section> with .anilist/.characters <img> elements",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output SVG path (default: target name with .out.svg)",
    )
    parser.add_argument(
        "-m",
        "--max-images",
        type=int,
        default=None,
        help="Optional cap on number of images from the source SVG",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        default=False,
        help="Prefer target <foreignObject> that already contains .anilist",
    )
    parser.add_argument("--width", type=int, default=36, help="Output <img> width")
    parser.add_argument("--height", type=int, default=54, help="Output <img> height")

    args = parser.parse_args()
    AniListSVG().inject(
        target_svg=args.svg,
        source_svg=args.source,
        output=args.output,
        max_images=args.max_images,
        width=args.width,
        height=args.height,
        merge=args.merge,
    )
