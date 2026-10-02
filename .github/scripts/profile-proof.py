"""Check profile resource/format structure, not universal browser appearance.

SVGs must be self-contained vectors: no foreignObject, processing
instructions, xml:base, scripts/event handlers, javascript: navigation, SMIL
animation, or CSS image resource functions.
Self-contained CSS animation is allowed. Raster animation remains supported
and every frame is decoded.
"""

from html.parser import HTMLParser
import math
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from markdown_it import MarkdownIt
from PIL import Image, ImageSequence


def decode_url(value):
    """Accept well-formed percent escapes and strict UTF-8 bytes."""
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise ValueError(f"URL contains a malformed percent escape: {value}")
    return unquote(value, errors="strict")


def srcset_candidates(value):
    """Collect URL-first tokens using HTML's srcset whitespace/comma rules.

    https://html.spec.whatwg.org/multipage/images.html#parse-a-srcset-attribute
    Descriptor validation below deliberately excludes unsupported descriptor forms.
    """
    whitespace = " \t\n\r\f"
    position = 0
    candidates = []
    while position < len(value):
        while position < len(value) and value[position] in whitespace + ",":
            position += 1
        start = position
        while position < len(value) and value[position] not in whitespace:
            position += 1
        url = value[start:position]
        if not url:
            break
        if url.endswith(","):
            candidates.append([url.rstrip(",")])
            continue
        start = position
        while position < len(value) and value[position] != ",":
            position += 1
        descriptors = re.findall(r"[^ \t\n\r\f]+", value[start:position])
        candidates.append([url, *descriptors])
        if position < len(value):
            position += 1
    return candidates


def reject_symlinks(path, repo):
    for node in (path, *path.parents):
        if not node.is_relative_to(repo):
            break
        if node.is_symlink():
            raise ValueError(f"GitHub image proof does not support symlink paths: {node.relative_to(repo)}")


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
            candidates = srcset_candidates(attrs["srcset"])
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
    reject_symlinks(profile / "README.md", repo)
    readme = (profile / "README.md").read_text()
    parser.feed(MarkdownIt("commonmark", {"html": True}).render(readme))
    if not parser.image_count or not parser.references:
        raise ValueError("Profile must render at least one img element")
    assets = set()
    for reference in parser.references:
        if "\\" in reference or "\\" in decode_url(reference):
            raise ValueError(f"Backslashes are unsupported in image URLs: {reference}")
        url = urlsplit(reference)
        if url.scheme or url.netloc:
            raise ValueError(f"Profile image must be a local asset: {reference}")
        decoded_path = decode_url(url.path)
        if decoded_path.startswith("/") or Path(decoded_path).is_absolute():
            raise ValueError(f"Profile image URL must be profile-relative: {reference}")
        raw_asset = profile / decoded_path
        reject_symlinks(raw_asset, repo)
        asset = raw_asset.resolve()
        if not asset.is_relative_to(profile.resolve()):
            raise ValueError(f"Image escapes profile directory: {reference}")
        if not asset.is_file() or asset.stat().st_size == 0:
            raise ValueError(f"Missing or empty profile asset: {reference}")
        assets.add(asset)
    svgs = {asset for asset in assets if asset.suffix.lower() == ".svg"}
    svgs.update((profile / "images").rglob("*.svg"))
    for svg in svgs:
        reject_symlinks(svg, repo)
        tree = ET.iterparse(svg, events=("pi",))
        for _, instruction in tree:
            raise ValueError(f"Self-contained vector SVGs do not support processing instructions: {svg.relative_to(repo)}")
        root = tree.root
        if root.tag != "{http://www.w3.org/2000/svg}svg":
            raise ValueError(f"Invalid SVG root: {svg.relative_to(repo)}")
        id_values = [node.attrib["id"] for node in root.iter() if "id" in node.attrib]
        ids = set(id_values)
        if len(ids) != len(id_values):
            raise ValueError(f"SVG IDs must be unique: {svg.relative_to(repo)}")
        if "" in ids:
            raise ValueError(f"SVG IDs must not be empty: {svg.relative_to(repo)}")

        def fragment(value, trim=True):
            if trim:
                value = value.strip("".join(chr(c) for c in range(33)))
            if not value.startswith("#") or len(value) == 1 or decode_url(value[1:]) not in ids:
                raise ValueError(f"SVG resources must reference existing in-document IDs: {svg.relative_to(repo)}: {value}")

        def uncomment(value):
            """Mask CSS comments and ordinary strings; preserve URL tokens."""
            parts = []
            position = 0
            while position < len(value):
                if value.startswith("/*", position):
                    end = value.find("*/", position + 2)
                    if end == -1:
                        raise ValueError(f"Unclosed SVG CSS comment: {svg.relative_to(repo)}")
                    # CSS comments separate tokens; never concatenate identifiers.
                    parts.append(" ")
                    position = end + 2
                elif value[position] in {"'", '"'}:
                    end = value.find(value[position], position + 1)
                    if end == -1:
                        raise ValueError(f"Unmatched SVG CSS string quote: {svg.relative_to(repo)}")
                    parts.append(" ")
                    position = end + 1
                elif value[position:position + 4].lower() == "url(" and (position == 0 or not (value[position - 1].isalnum() or value[position - 1] in "_-")):
                    end = position + 4
                    quote = None
                    quoted = False
                    token_started = False
                    while end < len(value):
                        char = value[end]
                        if quote:
                            if char == quote:
                                quote = None
                        elif value.startswith("/*", end) and (quoted or not token_started):
                            comment_end = value.find("*/", end + 2)
                            if comment_end == -1:
                                raise ValueError(f"Unclosed SVG CSS comment: {svg.relative_to(repo)}")
                            end = comment_end + 2
                            continue
                        elif char in {"'", '"'}:
                            quote = char
                            quoted = True
                            token_started = True
                        elif char == ")":
                            break
                        elif char not in " \t\n\r\f":
                            token_started = True
                        end += 1
                    if end == len(value):
                        raise ValueError(f"Unclosed SVG CSS url() resource: {svg.relative_to(repo)}")
                    parts.append(value[position:end + 1])
                    position = end + 1
                else:
                    parts.append(value[position])
                    position += 1
            return "".join(parts)

        def css(value):
            value = uncomment(value)
            if "\\" in value or re.search(r"@import\b", value, re.I):
                raise ValueError(f"SVG CSS imports/escapes are unsupported: {svg.relative_to(repo)}")
            if re.search(r"(?:^|[^\w-])(?:-webkit-)?(?:image|image-set|cross-fade|src)\s*\(", value, re.I):
                raise ValueError(f"Self-contained vector SVGs do not support CSS image/src resource functions: {svg.relative_to(repo)}")
            def skip_gap(position):
                while position < len(value):
                    if value[position] in " \t\n\r\f":
                        position += 1
                    elif value.startswith("/*", position):
                        end = value.find("*/", position + 2)
                        if end == -1:
                            raise ValueError(f"Unclosed SVG CSS comment: {svg.relative_to(repo)}")
                        position = end + 2
                    else:
                        break
                return position

            position = 0
            url_start = re.compile(r"(?<![\w-])url\(", re.I)
            while match := url_start.search(value, position):
                start = skip_gap(match.end())
                if start < len(value) and value[start] in {"'", '"'}:
                    quote = value[start]
                    quote_end = value.find(quote, start + 1)
                    if quote_end == -1:
                        raise ValueError(f"Unmatched SVG CSS URL quote: {svg.relative_to(repo)}")
                    resource = value[start + 1:quote_end]
                    end = skip_gap(quote_end + 1)
                    if end == len(value) or value[end] != ")":
                        raise ValueError(f"Unclosed SVG CSS url() resource: {svg.relative_to(repo)}")
                else:
                    end = value.find(")", start)
                    if end == -1:
                        raise ValueError(f"Unclosed SVG CSS url() resource: {svg.relative_to(repo)}")
                    resource = value[start:end].strip(" \t\n\r\f")
                fragment(resource, trim=False)
                position = end + 1


        # SVG presentation attributes with resource-valued CSS syntax:
        # https://www.w3.org/TR/SVG2/styling.html#PresentationAttributes
        resource_attributes = {
            "fill", "stroke", "filter", "clip-path", "mask", "marker",
            "marker-start", "marker-mid", "marker-end", "cursor", "color-profile",
        }
        for node in root.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "foreignObject":
                raise ValueError(f"Self-contained vector SVGs do not support foreignObject: {svg.relative_to(repo)}")
            if tag in {"script", "set", "animate", "animateMotion", "animateTransform", "discard"}:
                raise ValueError(f"Self-contained vector SVGs do not support dynamic markup: {svg.relative_to(repo)}: {tag}")
            for name, value in node.attrib.items():
                if name == "{http://www.w3.org/XML/1998/namespace}base":
                    raise ValueError(f"Self-contained vector SVGs do not support xml:base: {svg.relative_to(repo)}")
                local_name = name.rsplit("}", 1)[-1]
                if local_name.lower().startswith("on"):
                    raise ValueError(f"SVG event-handler scripting is unsupported: {svg.relative_to(repo)}: {local_name}")
                if local_name == "href" and urlsplit(value).scheme.lower() == "javascript":
                    raise ValueError(f"SVG javascript: navigation is unsupported: {svg.relative_to(repo)}")
                if local_name in {"href", "src"} and tag != "a":
                    fragment(value)
                if local_name == "style" or local_name in resource_attributes or re.search(r"(?:url|src|(?:-webkit-)?image(?:-set)?|cross-fade)\s*\(", value, re.I):
                    css(value)
            if tag == "style":
                css("".join(node.itertext()))
    rasters = assets - svgs
    web_formats = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF", ".webp": "WEBP", ".avif": "AVIF"}
    for raster in rasters:
        with Image.open(raster) as image:
            if web_formats.get(raster.suffix.lower()) != image.format:
                raise ValueError(f"Raster format must match a supported web suffix: {raster.relative_to(repo)}")
            image.verify()
        with Image.open(raster) as image:
            for frame in ImageSequence.Iterator(image):
                frame.load()
    print(f"Profile proof: {len(assets)} referenced assets; {len(svgs)} valid SVGs; {len(rasters)} decoded rasters")


if __name__ == "__main__":
    main()
