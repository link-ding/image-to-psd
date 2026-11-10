#!/usr/bin/env python3
"""Export a layered PSD from registered character parts; semantic segmentation is supplied by the caller."""
import argparse
import copy
import hashlib
import io
import json
import re
from pathlib import Path
import xml.etree.ElementTree as ET

SVG = 'http://www.w3.org/2000/svg'
INK = 'http://www.inkscape.org/namespaces/inkscape'
NAMES = ['back hair', 'front hair', 'face', 'ears', 'earwear', 'eyebrow',
         'eyewhite', 'irides', 'eyelash', 'nose', 'mouth', 'headwear', 'neck',
         'topwear', 'handwear', 'bottomwear', 'legwear', 'footwear', 'objects']
HANDWEAR_SPLIT = ('handwear back', 'handwear front')
SPLIT_NAMES = (set(NAMES) - {'handwear'}) | set(HANDWEAR_SPLIT)
ET.register_namespace('', SVG)
ET.register_namespace('inkscape', INK)

def tag(name):
    return '{' + SVG + '}' + name

def label(group):
    return group.get('{' + INK + '}label') or group.get('data-layer-name')

def check_layer_order(names):
    if len(names) != len(set(names)):
        raise ValueError('Duplicate layer names')
    if set(names) == set(NAMES):
        return
    if set(names) == SPLIT_NAMES:
        back, front = HANDWEAR_SPLIT
        if names.index(back) < names.index('objects') < names.index(front):
            return
    raise ValueError('Require the 19 standard layers, or handwear back < objects < handwear front in place of handwear')

def load_svg(path):
    root = ET.parse(path).getroot()
    if root.tag != tag('svg'):
        raise ValueError('Expected an SVG document')
    ids = [n.get('id') for n in root.iter() if n.get('id')]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate SVG IDs; prefix and rewrite references first')
    for n in root.iter():
        if n.tag in [tag('image'), tag('script'), tag('foreignObject')]:
            raise ValueError('Expected self-contained vectors without images or scripts')
        for key, value in n.attrib.items():
            if key.rsplit('}', 1)[-1] == 'href' and not value.startswith('#'):
                raise ValueError('External SVG references are not allowed')
            for target in re.findall(r'url\((.*?)\)', value):
                if not target.strip(' "\'').startswith('#'):
                    raise ValueError('External SVG resources are not allowed')
    width = float(root.get('width', '0'))
    height = float(root.get('height', '0'))
    if width <= 0 or height <= 0 or width != int(width) or height != int(height):
        raise ValueError('Set positive integer width and height in source pixels')
    layers = [g for g in root if g.tag == tag('g') and label(g)]
    names = [label(g) for g in layers]
    check_layer_order(names)
    if any(g.tag == tag('g') and g not in layers for g in root):
        raise ValueError('All painted top-level groups must be named layers')
    if any(n.tag not in [tag('g'), tag('defs'), tag('title'), tag('desc'),
                          tag('metadata'), tag('style')] for n in root):
        raise ValueError('Move top-level artwork into named layers')
    # All shared resources belong in root defs; sibling artwork references would
    # change when rendering a single layer and must be resolved by the caller.
    return root, layers, (int(width), int(height))

def render(root, group=None):
    import cairosvg
    from PIL import Image
    doc = copy.deepcopy(root)
    if group is not None:
        for child in list(doc):
            if child.tag == tag('g'):
                doc.remove(child)
        doc.append(copy.deepcopy(group))
    return Image.open(io.BytesIO(cairosvg.svg2png(bytestring=ET.tostring(doc)))).convert('RGBA')

def read_manifest(path):
    manifest_path = Path(path)
    spec = json.loads(manifest_path.read_text(encoding='utf-8'))
    width, height = spec['width'], spec['height']
    if any(type(v) is not int or v <= 0 for v in (width, height)):
        raise ValueError('Width and height must be positive integers')
    supplied = spec['layers']
    names = [entry['name'] for entry in supplied]
    if set(names) & set(HANDWEAR_SPLIT):
        # The interleaved stack is intentional. Require both passes and objects
        # to be listed, even when one of the passes is empty.
        if not set((*HANDWEAR_SPLIT, 'objects')).issubset(names) or 'handwear' in names:
            raise ValueError('List handwear back, objects and handwear front instead of handwear')
        allowed = [name for name in NAMES if name != 'handwear']
    else:
        allowed = NAMES
    layers = supplied + [{'name': name} for name in allowed if name not in names]
    check_layer_order([entry['name'] for entry in layers])
    return (width, height), layers, manifest_path.parent


def read_part(directory, entry, size):
    from PIL import Image
    if not entry.get('png'):
        return Image.new('RGBA', size)
    with Image.open(directory / entry['png']) as source:
        if source.format != 'PNG' or source.size != size:
            raise ValueError(f"{entry['name']}: expected a PNG registered to the full canvas")
        return source.convert('RGBA')


def composite_error(first, second):
    import numpy as np
    first = np.asarray(first.convert('RGBa'), dtype=np.int16)
    second = np.asarray(second.convert('RGBa'), dtype=np.int16)
    return float(np.abs(first - second).mean())


def layer_digest(im):
    # Invisible RGB has no effect on the comparison.
    from PIL import Image
    alpha = im.getchannel('A')
    visible_rgb = Image.new('RGB', im.size)
    visible_rgb.paste(im, mask=alpha.point([0] + [255] * 255))
    digest = hashlib.sha256(alpha.tobytes())
    digest.update(visible_rgb.tobytes())
    return digest.digest()


def psd_command(args):
    import numpy as np
    from PIL import Image
    from psd_tools import PSDImage
    from psd_tools.api.layers import PixelLayer
    from psd_tools.constants import Compression

    source_composite = None
    if args.command == 'psd-png':
        size, entries, directory = read_manifest(args.source)
        parts = ((entry['name'], read_part(directory, entry, size)) for entry in entries)
        source_type = 'registered_png'
    else:
        root, groups, size = load_svg(args.source)
        parts = ((label(group), render(root, group)) for group in groups)
        source_composite = render(root)
        source_type = 'svg'

    psd = PSDImage.new('RGBA', size, (0, 0, 0, 0), depth=8)
    expected = Image.new('RGBA', size)
    records = []
    digests = []
    for name, im in parts:
        alpha = im.getchannel('A')
        if alpha.getextrema() == (255, 255):
            raise ValueError(f'{name}: fully opaque full-canvas anatomy layer; inspect segmentation')
        layer = PixelLayer.frompil(im, psd, name=name, compression=Compression.RLE)
        # Keep soft edges in the pixel alpha. The independent mask marks the
        # support of the part without attenuating partially opaque pixels again.
        layer.create_mask(alpha.point([0] + [255] * 255), top=0, left=0,
                          compression=Compression.RLE)
        expected = Image.alpha_composite(expected, im)
        digests.append(layer_digest(im))
        records.append({'name': name, 'alpha_bounds': alpha.getbbox(),
                        'nonzero_pixels': int(np.count_nonzero(np.asarray(alpha)))})

    if source_composite is not None:
        isolation_error = composite_error(source_composite, expected)
        if isolation_error > 0.5:
            raise ValueError(f'Isolated layers differ from complete SVG: {isolation_error:.3f}')

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    # The signed layer count tells PSD readers that the fourth merged
    # channel is transparency. Render that preview over a transparent backdrop.
    psd._record.layer_and_mask_information.layer_info.layer_count = -len(records)
    psd._background_color = None
    psd.save(args.output)
    reopened = PSDImage.open(args.output)
    if reopened.size != size or [layer.name for layer in reopened] != [r['name'] for r in records]:
        raise ValueError('PSD dimensions or layer ordering changed')
    for layer, digest in zip(reopened, digests):
        if not layer.has_mask() or layer.bbox != (0, 0, *size) or layer.mask.bbox != layer.bbox:
            raise ValueError(f'Invalid mask or registration: {layer.name}')
        im = layer.topil().convert('RGBA')
        if layer_digest(im) != digest:
            raise ValueError(f'PSD pixel or alpha round-trip changed: {layer.name}')
        support = im.getchannel('A').point([0] + [255] * 255)
        if layer.mask.topil().tobytes() != support.tobytes():
            raise ValueError(f'PSD mask changed: {layer.name}')

    composite = reopened.composite(force=True).convert('RGBA')
    error = composite_error(composite, expected)
    if error > 0.5:
        raise ValueError(f'PSD composite verification failed: {error:.3f}')
    embedded_preview = reopened.topil()
    if (embedded_preview is None or embedded_preview.mode != 'RGBA'
            or composite_error(embedded_preview, expected) > 0.5):
        raise ValueError('PSD embedded preview lost transparency or changed color')
    report = {'width': size[0], 'height': size[1], 'layers': records,
              'source': source_type, 'mask_count': len(reopened),
              'psd_premultiplied_mean_error': error,
              'mask_policy': 'soft pixel alpha with an independent binary support mask',
              'target_application_verified': False}
    if source_composite is not None:
        report['svg_isolation_mean_error'] = isolation_error
        report['psd_vs_original_svg_mean_error'] = composite_error(composite, source_composite)
    if args.preview:
        composite.save(args.preview)
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('psd', 'psd-png'):
        p = sub.add_parser(command)
        p.add_argument('source', metavar='svg' if command == 'psd' else 'manifest')
        p.add_argument('output')
        p.add_argument('--report')
        p.add_argument('--preview')
        p.set_defaults(run=psd_command)
    args = parser.parse_args()
    args.run(args)

if __name__ == '__main__':
    main()
