"""Validate the organization profile's local image contract."""

from html.parser import HTMLParser
import math
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from markdown_it import MarkdownIt
from PIL import Image, ImageSequence


class Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []
        self.image_count = 0

    def handle_starttag(self, tag, attrs):
        if tag not in {"img", "source"}:
            return
        url_attrs = [name for name, _ in attrs if name in {"src", "srcset"}]
        if len(url_attrs) != len(set(url_attrs)):
            raise ValueError(f"Duplicate image URL attributes on {tag}")
        attrs = dict(attrs)
        if not url_attrs or any(not attrs[name] or not attrs[name].strip() for name in url_attrs):
            raise ValueError(f"Missing or empty image URL attributes on {tag}")
        if tag == "img":
            self.image_count += 1
        if attrs.get("src"):
            self.references.append(attrs["src"])
        if attrs.get("srcset"):
            candidates = [candidate.split() for candidate in attrs["srcset"].split(",") if candidate.strip()]
            if not candidates:
                raise ValueError("Image srcset must contain a usable candidate")
            descriptors = set()
            kinds = set()
            for candidate in candidates:
                if len(candidate) > 2:
                    raise ValueError("Image srcset candidate has multiple descriptors")
                kind, value = "x", 1.0
                if len(candidate) == 2:
                    descriptor = candidate[1]
                    if re.fullmatch(r"[0-9]+w", descriptor):
                        kind, value = "w", int(descriptor[:-1])
                    elif re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?x", descriptor):
                        value = float(descriptor[:-1])
                    else:
                        raise ValueError(f"Malformed image srcset descriptor: {descriptor}")
                if value <= 0 or not math.isfinite(value) or (kind, value) in descriptors:
                    raise ValueError("Image srcset descriptors must be positive, finite, and unique")
                kinds.add(kind)
                descriptors.add((kind, value))
                self.references.append(candidate[0])
            if len(kinds) > 1:
                raise ValueError("Image srcset cannot mix width and density descriptors")


def main():
    repo = Path(__file__).resolve().parents[2]
    profile = repo / "profile"
    parser = Images()
    readme = (profile / "README.md").read_text()
    parser.feed(MarkdownIt("commonmark", {"html": True}).render(readme))
    if not parser.image_count or not parser.references:
        raise ValueError("Profile must render at least one img element")
    assets = set()
    for reference in parser.references:
        if "\\" in reference or "\\" in unquote(reference):
            raise ValueError(f"Backslashes are unsupported in image URLs: {reference}")
        url = urlsplit(reference)
        if url.scheme or url.netloc:
            raise ValueError(f"Profile image must be a local asset: {reference}")
        asset = (profile / unquote(url.path)).resolve()
        if not asset.is_relative_to(profile.resolve()):
            raise ValueError(f"Image escapes profile directory: {reference}")
        if not asset.is_file() or asset.stat().st_size == 0:
            raise ValueError(f"Missing or empty profile asset: {reference}")
        assets.add(asset)
    svgs = {asset for asset in assets if asset.suffix.lower() == ".svg"}
    svgs.update((profile / "images").rglob("*.svg"))
    for svg in svgs:
        root = ET.parse(svg).getroot()
        if root.tag != "{http://www.w3.org/2000/svg}svg":
            raise ValueError(f"Invalid SVG root: {svg.relative_to(repo)}")
        ids = {node.attrib["id"] for node in root.iter() if "id" in node.attrib}

        def fragment(value):
            value = value.strip()
            if not value.startswith("#") or value[1:] not in ids:
                raise ValueError(f"SVG resources must reference existing in-document IDs: {svg.relative_to(repo)}: {value}")

        def css(value):
            if "\\" in value or re.search(r"@import\b", value, re.I):
                raise ValueError(f"SVG CSS imports/escapes are unsupported: {svg.relative_to(repo)}")
            for match in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", value, re.I | re.S):
                fragment(match.group(2))

        for node in root.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            for name, value in node.attrib.items():
                local_name = name.rsplit("}", 1)[-1]
                if local_name in {"href", "src"} and tag != "a":
                    fragment(value)
                if local_name == "style" or "url(" in value.lower():
                    css(value)
            if tag == "style":
                css("".join(node.itertext()))
    rasters = assets - svgs
    for raster in rasters:
        with Image.open(raster) as image:
            image.verify()
        with Image.open(raster) as image:
            for frame in ImageSequence.Iterator(image):
                frame.load()
    print(f"Profile proof: {len(assets)} referenced assets; {len(svgs)} valid SVGs; {len(rasters)} decoded rasters")


if __name__ == "__main__":
    main()
