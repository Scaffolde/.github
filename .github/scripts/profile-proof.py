"""Validate the organization profile's local image contract."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET


class Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"img", "source"}:
            return
        attrs = dict(attrs)
        if attrs.get("src"):
            self.references.append(attrs["src"])
        if attrs.get("srcset"):
            self.references.extend(
                candidate.strip().split()[0]
                for candidate in attrs["srcset"].split(",")
                if candidate.strip()
            )


def main():
    repo = Path(__file__).resolve().parents[2]
    profile = repo / "profile"
    parser = Images()
    parser.feed((profile / "README.md").read_text())
    if not parser.references:
        raise ValueError("Profile must reference at least one image")
    assets = set()
    for reference in parser.references:
        url = urlsplit(reference)
        if url.scheme or url.netloc:
            raise ValueError(f"Profile image must be a local asset: {reference}")
        asset = (profile / unquote(url.path)).resolve()
        if not asset.is_relative_to(profile.resolve()):
            raise ValueError(f"Image escapes profile directory: {reference}")
        if not asset.is_file() or asset.stat().st_size == 0:
            raise ValueError(f"Missing or empty profile asset: {reference}")
        assets.add(asset)
    svgs = list((profile / "images").glob("*.svg"))
    for svg in svgs:
        if ET.parse(svg).getroot().tag != "{http://www.w3.org/2000/svg}svg":
            raise ValueError(f"Invalid SVG root: {svg.relative_to(repo)}")
    print(f"Profile proof: {len(assets)} referenced assets; {len(svgs)} valid SVGs")


if __name__ == "__main__":
    main()
