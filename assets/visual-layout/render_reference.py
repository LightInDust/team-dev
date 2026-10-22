"""Small synthetic ownership/provenance example, not a scientific renderer.

Run: existing-matplotlib-python render_reference.py -v
Adapt the pattern and independent expectations to the project's real contract.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from check_layout import assert_layout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def frozen_json(path, *, expected_path):
    """Small fixture only: hash before loading; check consumed bytes and final file.

    Final hashes detect persistent changes, not transient external writes (ABA).
    Large/multi-file projects need their own authorized loaders and snapshot policy.
    """
    path = Path(path).resolve(strict=True)
    if path != Path(expected_path).resolve(strict=True):
        raise ValueError('Source identity mismatch')
    before = digest(path)
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != before:
        raise ValueError('Input changed during loading')
    data = json.loads(payload)
    yield data, before
    if digest(path) != before:
        raise ValueError('Input changed during rendering')


@contextmanager
def owned_figure():
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(12, 8), dpi=100)
    try:
        yield fig  # Populate/layout/save all happen inside the owning scope.
    finally:
        plt.close(fig)


def populate(fig, data):
    import numpy as np
    raw = np.asarray(data['model_raw'], dtype=float)
    kept = raw[data['keep_indices']]
    displayed = kept[data['display_indices']]
    captured = np.asarray(data['captured'], dtype=float)
    annotation = np.asarray(data['annotation'], dtype=float)
    # Every contracted layer before sampling, with the fixture's explicit margin.
    union = np.concatenate([captured, kept, annotation])
    bounds = np.stack([union.min(axis=0), union.max(axis=0)], axis=1)
    margin = (bounds[:, 1] - bounds[:, 0]) * data['margin_fraction']
    bounds += np.stack([-margin, margin], axis=1)
    # XY below XZ; image upper left, metadata lower left.
    axes = [fig.add_subplot(2, 2, position) for position in (4, 2, 1)]
    for ax, indices, name in zip(axes[:2], ([0, 1], [0, 2]), ('XY', 'XZ')):
        for points, label in ((captured, 'Captured'), (displayed, 'Model'), (annotation, 'Annotation')):
            ax.scatter(*points[:, indices].T, label=label)
        ax.set(xlim=bounds[indices[0]], ylim=bounds[indices[1]], title=name,
               xlabel='x (m)', ylabel=f'{name[-1].lower()} (m)')
        ax.set_aspect('equal', adjustable='box')
    width, height = data['image_size_wh']
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[::2, 1::2, 0] = 255  # Synthetic sharp pixel pattern, not inferred image content.
    image[-1, 0] = [0, 255, 0]
    extent = (-.5, width-.5, height-.5, -.5)
    image_artist = axes[2].imshow(image, aspect='equal', origin='upper', extent=extent,
                                  interpolation='nearest', resample=False)
    axes[2].set_xlim(extent[:2])
    axes[2].set_ylim(extent[2:])
    axes[2].set_autoscale_on(False)
    axes[2].set_title('Image orientation and aspect preserved')
    uv = np.asarray(data['pixel_points'])
    # Pixel-coordinate domain is half-open; continuous layout bboxes differ.
    valid = ((uv >= 0) & (uv < [width, height])).all(axis=1)
    axes[2].scatter(*uv[valid].T, color='lime')
    counts = [len(raw), len(kept), len(displayed)]
    metadata = fig.text(.1, .33, f"Source: {data['source_id']}\nOutput: {data['output_id']}\n"
                        f"Raw / kept / displayed: {counts}")
    handles, labels = axes[0].get_legend_handles_labels()
    legend = fig.legend(handles, labels, loc='lower center', bbox_to_anchor=(.5, .015), ncol=3)
    fig.subplots_adjust(left=.08, right=.96, bottom=.14, top=.91, hspace=.55, wspace=.35)
    return {'source_id': data['source_id'], 'output_id': data['output_id'],
            'counts': counts, 'bounds': bounds, 'pixel_mask': valid,
            'axes': axes, 'metadata': metadata, 'legend': legend, 'image_artist': image_artist}


def separation_pairs(state):
    xy, xz, _ = state['axes']
    return [(state['metadata'], state['legend']), (xz.xaxis.label, xy.title)]


def populate_unavailable(fig, card):
    # A text-only branch has no fake zero-valued data, legend or unused ticks.
    title = fig.text(.08, .9, textwrap.fill(card['title'], 55, break_on_hyphens=False,
                                        break_long_words=False), va='top', fontsize=18)
    body = fig.text(.08, .7, textwrap.fill(card['body'], 75, break_on_hyphens=False,
                                       break_long_words=False), va='top', fontsize=13)
    return {'title': title, 'body': body}


def render(path, output, inspect=None, *, expected_path, kind='comparison'):
    import matplotlib as mpl
    if kind not in ('comparison', 'unavailable'):
        raise ValueError('Unsupported example layout')
    source = Path(path).resolve(strict=True)
    with frozen_json(source, expected_path=expected_path) as (fixture, before):
        with owned_figure() as fig:
            if kind == 'unavailable':
                state = populate_unavailable(fig, fixture['unavailable_card'])
                layout = assert_layout(fig, require_legend=False,
                                       separate_pairs=[(state['title'], state['body'])], gap_px=2)
            else:
                state = populate(fig, fixture['input'])
                layout = assert_layout(fig, separate_pairs=separation_pairs(state), gap_px=2)
            if inspect:
                inspect(fig, state, fixture['unavailable_card'] if kind == 'unavailable' else fixture['expected'])
            with mpl.rc_context({'savefig.bbox': None}):
                fig.savefig(output, dpi=fig.dpi, bbox_inches=None)
    # Only returned after final input verification and successful close/save.
    return {'source_path': str(source), 'source_sha256': before, 'content_status': kind, 'layout': layout}


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        import matplotlib.pyplot as plt
        self.temp = tempfile.TemporaryDirectory(prefix='team-dev-reference-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'input.json'
        self.path.write_bytes(Path(__file__).with_name('asymmetric.json').read_bytes())
        self.output = Path(self.temp.name)/'result.png'
        self.other = plt.figure()  # Must survive both normal and failing renders.
        self.addCleanup(plt.close, self.other)
        self.original_figures = plt.get_fignums()

    def check_data(self, fig, state, expected):
        import numpy as np
        self.assertEqual(state['source_id'], expected['source_id'])
        self.assertEqual(state['output_id'], expected['output_id'])
        self.assertEqual(state['counts'], expected['counts_raw_kept_displayed'])
        for line in expected['metadata_lines']:
            self.assertIn(line, state['metadata'].get_text().splitlines())
        np.testing.assert_array_equal(state['bounds'], expected['bounds_xyz'])
        np.testing.assert_array_equal(state['pixel_mask'], expected['pixel_in_frame'])
        np.testing.assert_array_equal(state['axes'][2].collections[0].get_offsets(), expected['plotted_pixels'])
        artist = state['image_artist']
        self.assertEqual(artist.get_interpolation(), 'nearest')
        self.assertFalse(artist.get_resample())
        self.assertEqual(artist.origin, 'upper')
        image_height, image_width = artist.get_array().shape[:2]
        self.assertEqual(tuple(artist.get_extent()), (-.5, image_width-.5, image_height-.5, -.5))
        image_ax = state['axes'][2]
        np.testing.assert_array_equal(image_ax.get_xlim(), expected['image_xlim'])
        np.testing.assert_array_equal(image_ax.get_ylim(), expected['image_ylim'])
        self.assertFalse(image_ax.get_autoscalex_on())
        self.assertFalse(image_ax.get_autoscaley_on())
        # Extent alone can remain correct while scatter expands the viewport.
        xmin, xmax = expected['image_xlim']
        ymax, ymin = expected['image_ylim']
        corners = image_ax.transData.transform([[xmin, ymax], [xmax, ymin]])
        np.testing.assert_allclose(corners.ravel(), image_ax.get_window_extent().extents,
                                   rtol=0, atol=1e-8)
        np.testing.assert_array_equal(artist.get_array()[0, 1], [255, 0, 0])
        np.testing.assert_array_equal(artist.get_array()[-1, 0], [0, 255, 0])
        # The fixture's literal XY/XZ oracles do not call the renderer's mapping.
        for ax, name, limits in zip(state['axes'][:2], ('xy', 'xz'),
                                   (([-25, 85], [-38.25, 33.25]), ([-25, 85], [-49.25, 44.25]))):
            np.testing.assert_array_equal(ax.get_xlim(), limits[0])
            np.testing.assert_array_equal(ax.get_ylim(), limits[1])
            np.testing.assert_array_equal(ax.collections[1].get_offsets(), expected[f'model_{name}'])
            np.testing.assert_array_equal(ax.collections[2].get_offsets(), expected[f'annotation_{name}'])
        for ax in state['axes']:  # Numeric transformed scale, not an aspect-string check.
            origin, x, y = ax.transData.transform([[0, 0], [1, 0], [0, 1]])
            self.assertAlmostEqual(np.linalg.norm(x-origin), np.linalg.norm(y-origin))

    def test_real_export_with_independent_asymmetric_expectations(self):
        import matplotlib.pyplot as plt
        from matplotlib import image
        result = render(self.path, self.output, self.check_data, expected_path=self.path)
        self.assertEqual(image.imread(self.output).shape[:2], (800, 1200))
        self.assertEqual(result['source_sha256'], digest(self.path))
        self.assertEqual(plt.get_fignums(), self.original_figures)

    def test_original_pixels_ignore_smoothing_rcparams(self):
        import matplotlib as mpl
        from matplotlib import image
        with mpl.rc_context({'image.interpolation': 'bilinear', 'image.resample': True}):
            render(self.path, self.output, self.check_data, expected_path=self.path)
        self.assertEqual(image.imread(self.output).shape[:2], (800, 1200))

    def test_cleanup_at_construction_layout_and_save_failure(self):
        import matplotlib.pyplot as plt
        targets = [__name__ + '.populate', __name__ + '.assert_layout', 'matplotlib.figure.Figure.savefig']
        for target in targets:
            with self.subTest(stage=target), patch(target, side_effect=RuntimeError('injected')):
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    render(self.path, self.output, expected_path=self.path)
                self.assertEqual(plt.get_fignums(), self.original_figures)

    def test_correct_extent_with_expanded_overlay_viewport_rejects_export(self):
        import matplotlib.pyplot as plt
        populate_real = populate
        observed = {}
        def expanded(fig, data):
            state = populate_real(fig, data)
            image_ax = state['axes'][2]
            image_ax.autoscale(enable=True, axis='both')
            fig.canvas.draw()
            observed.update(extent=tuple(state['image_artist'].get_extent()),
                            xlim=image_ax.get_xlim(), ylim=image_ax.get_ylim())
            return state
        with patch(__name__ + '.populate', side_effect=expanded), patch('matplotlib.figure.Figure.savefig') as save:
            with self.assertRaisesRegex(AssertionError, 'Arrays are not equal'):
                render(self.path, self.output, self.check_data, expected_path=self.path)
            save.assert_not_called()
        # Fault setup must pass independently of the expected acceptance error.
        self.assertEqual(observed['extent'], (-.5, 11.5, 5.5, -.5))
        self.assertGreater(observed['xlim'][1], 11.5)
        self.assertGreater(observed['ylim'][0], 5.5)
        self.assertFalse(self.output.exists())
        self.assertEqual(plt.get_fignums(), self.original_figures)

    def test_input_change_blocks_receipt(self):
        import matplotlib.pyplot as plt
        original = self.path.read_bytes()
        parse = json.loads
        def change_during_decode(payload):
            self.path.write_text('{}')
            return parse(payload)
        with patch.object(json, 'loads', side_effect=change_during_decode):
            with self.assertRaisesRegex(ValueError, 'Input changed during rendering'):
                with frozen_json(self.path, expected_path=self.path):
                    pass
        self.path.write_bytes(original)
        def change(fig, state, expected):
            self.path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Input changed during rendering'):
            render(self.path, self.output, change, expected_path=self.path)
        self.assertEqual(plt.get_fignums(), self.original_figures)

    def test_drawn_ticks_and_declared_metadata_separation(self):
        for pair_index in (0, 1):
            with self.subTest(pair=pair_index), frozen_json(self.path, expected_path=self.path) as (fixture, _), owned_figure() as fig:
                state = populate(fig, fixture['input'])
                ax = state['axes'][0]
                ax.set_xticks([-99999, 0, 99999])
                ax.set_xlim(-20, 80)  # Only the middle tick is actually drawn.
                pairs = separation_pairs(state)
                assert_layout(fig, separate_pairs=pairs, gap_px=2)
                first, second = pairs[pair_index]
                box = second.get_window_extent(fig.canvas.get_renderer())
                position = fig.transFigure.inverted().transform(((box.x0+box.x1)/2, (box.y0+box.y1)/2))
                if pair_index == 0:
                    first.set_position(position)
                else:
                    state['axes'][1].xaxis.set_label_coords(*position, transform=fig.transFigure)
                first.set_verticalalignment('center')
                first.set_horizontalalignment('center')
                assert_layout(fig)  # Containment passes despite the collision.
                with self.assertRaisesRegex(AssertionError, 'Declared separate artists overlap'):
                    assert_layout(fig, separate_pairs=pairs, gap_px=2)

    def test_same_basename_and_bytes_do_not_prove_source_identity(self):
        alternate = Path(self.temp.name)/'other'/self.path.name
        alternate.parent.mkdir()
        alternate.write_bytes(self.path.read_bytes())
        with patch(__name__ + '.populate') as populate_spy:
            with self.assertRaisesRegex(ValueError, 'Source identity mismatch'):
                render(alternate, self.output, expected_path=self.path)
            populate_spy.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_unavailable_card_export_and_layout_failure_gate(self):
        import matplotlib.pyplot as plt
        from matplotlib import image
        def check_card(fig, state, expected):
            for field in ('title', 'body'):
                self.assertEqual(' '.join(state[field].get_text().split()), expected[field])
            self.assertEqual(fig.axes, [])
        result = render(self.path, self.output, check_card, expected_path=self.path, kind='unavailable')
        self.assertEqual(result['content_status'], 'unavailable')
        self.assertEqual(image.imread(self.output).shape[:2], (800, 1200))
        self.assertGreater(result['layout']['checked_artists'], 0)
        populate_card = populate_unavailable
        for component in ('title', 'body'):
            def clipped(fig, card):
                state = populate_card(fig, card)
                state[component].set_x(-.5)
                return state
            rejected = Path(self.temp.name)/f'rejected-{component}.png'
            with self.subTest(component=component), patch(__name__ + '.populate_unavailable', side_effect=clipped), \
                    patch('matplotlib.figure.Figure.savefig') as save:
                with self.assertRaisesRegex(AssertionError, 'leaves exported canvas'):
                    render(self.path, rejected, expected_path=self.path, kind='unavailable')
                save.assert_not_called()
                self.assertFalse(rejected.exists())
        self.assertEqual(plt.get_fignums(), self.original_figures)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='team-dev-mpl-cache-') as cache:
        os.environ['MPLCONFIGDIR'] = cache
        try:
            import matplotlib
        except ImportError:
            raise SystemExit('NOT VERIFIED: use an existing Matplotlib environment')
        matplotlib.use('Agg')
        unittest.main()
