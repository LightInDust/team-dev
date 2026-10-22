"""Fixed-canvas Matplotlib acceptance example; uses synthetic data only.

Run with an existing Python environment containing matplotlib.
Adapt assertions to the real renderer and its original test framework.
"""
import math
import os
from pathlib import Path
import tempfile
import unittest


def assert_layout(fig, *, extra_artists=(), separate_pairs=(), gap_px=0, require_legend=True):
    """Measure export pixels; comparison layouts require an outside legend by default."""
    if not math.isfinite(gap_px) or gap_px < 0:
        raise ValueError('Separation gap must be finite and nonnegative')
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    canvas = fig.bbox
    if require_legend and not fig.legends:
        raise AssertionError('Expected the comparison legend')
    axes = [ax for ax in fig.axes if ax.axison]
    artists = [*fig.texts, *extra_artists]
    for legend in fig.legends:
        artists += [legend, *legend.get_texts()]
        legend_box = legend.get_window_extent(renderer)
        for ax in axes:
            if legend_box.overlaps(ax.get_tightbbox(renderer)):
                raise AssertionError('Legend overlaps panel content/labels')
    for ax in axes:
        artists += [ax.title, ax.xaxis.label, ax.yaxis.label]
        for axis in (ax.xaxis, ax.yaxis):
            if not axis.get_visible():
                continue
            # Version-sensitive Matplotlib API: its RETURNED ticks are drawn.
            # Never discard the return value and enumerate all locator ticks.
            if not callable(getattr(axis, '_update_ticks', None)):
                raise RuntimeError('NOT VERIFIED: adapt drawn-tick access to the target Matplotlib')
            artists += [axis.get_offset_text()]
            for tick in axis._update_ticks():
                artists += [tick.label1, tick.label2]
    checked = 0
    for artist in artists:
        if not artist.get_visible() or (hasattr(artist, 'get_text') and not artist.get_text()):
            continue
        box = artist.get_window_extent(renderer)
        if not all(math.isfinite(v) for v in box.extents):
            raise AssertionError('Nonfinite artist extent')
        if box.x0 < canvas.x0 or box.y0 < canvas.y0 or box.x1 > canvas.x1 or box.y1 > canvas.y1:
            raise AssertionError('Required text/legend leaves exported canvas')
        checked += 1
    for heading in fig.texts:
        for ax in axes:
            if heading.get_window_extent(renderer).overlaps(ax.title.get_window_extent(renderer)):
                raise AssertionError('Figure heading overlaps panel title')
    for first, second in separate_pairs:
        if (first.get_visible() and second.get_visible()
                and first.get_window_extent(renderer).padded(gap_px).overlaps(second.get_window_extent(renderer))):
            raise AssertionError('Declared separate artists overlap')
    return {'width': canvas.width, 'height': canvas.height, 'checked_artists': checked}


class LayoutExampleTests(unittest.TestCase):
    def figure(self, detail=False, anchor=.02):
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(2 if detail else 3, 2 if detail else 6,
                                 figsize=(16, 10) if detail else (30, 16), dpi=100)
        self.addCleanup(plt.close, fig)
        for i, ax in enumerate(axes.flat):
            ax.plot([0, 1], [0, 1], label='Reference')
            ax.plot([0, 1], [1, 0], label='Candidate')
            ax.set_title(f'Object {i // 2 + 1} [{"XY" if i % 2 == 0 else "XZ"}]', fontsize=8)
            ax.set_xlabel('x (m)'); ax.set_ylabel('y (m)')
        fig.subplots_adjust(left=.06, right=.98, bottom=.14, top=.88, hspace=.6, wspace=.6)
        fig.suptitle('Reference comparison', y=.98)
        handles, labels = axes.flat[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc='lower center', bbox_to_anchor=(.5, anchor),
                   ncol=2, frameon=False, fontsize=11)
        return fig

    def test_complete_eleven_file_layout_with_actual_save(self):
        import matplotlib as mpl
        from matplotlib import image as mpimg
        with tempfile.TemporaryDirectory(prefix='team-dev-layout-') as td:
            files = []
            for i in range(11):
                fig = self.figure(detail=i >= 2)
                receipt = assert_layout(fig)
                path = Path(td)/f'figure-{i}.png'
                # The checks above describe this exact, fixed-size canvas/export.
                with mpl.rc_context({'savefig.bbox': None}):
                    fig.savefig(path, dpi=fig.dpi, bbox_inches=None)
                pixels = mpimg.imread(path)
                self.assertEqual(pixels.shape[:2], (int(receipt['height']), int(receipt['width'])))
                self.assertGreater(receipt['checked_artists'], 0)
                files.append(path)
                import matplotlib.pyplot as plt
                plt.close(fig)
            self.assertEqual(len(files), 11)

    def test_negative_legend_anchors_are_rejected(self):
        for detail, anchor in ((False, -.01), (True, -.02)):
            with self.subTest(detail=detail), self.assertRaisesRegex(AssertionError, 'leaves exported canvas'):
                assert_layout(self.figure(detail, anchor))

    def test_heading_collision_is_rejected(self):
        fig = self.figure()
        fig.canvas.draw()
        box = fig.axes[0].title.get_window_extent(fig.canvas.get_renderer())
        center = fig.transFigure.inverted().transform(((box.x0+box.x1)/2, (box.y0+box.y1)/2))
        fig.texts[0].set_position(center)
        fig.texts[0].set_verticalalignment('center')
        with self.assertRaisesRegex(AssertionError, 'heading overlaps'):
            assert_layout(fig)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='team-dev-mpl-cache-') as cache:
        os.environ['MPLCONFIGDIR'] = cache
        try:
            import matplotlib
        except ImportError:
            raise SystemExit('NOT VERIFIED: matplotlib is absent; use the existing target rendering environment')
        matplotlib.use('Agg')
        unittest.main()
