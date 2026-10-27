"""Check layouts where a geometric grouping mistake would be costly."""
import unittest

from paper_agent.tools.figure_grouping import group_page


def visual(box, caption=None, caption_box=None):
    b = {'type': 'image', 'bbox': box, 'content': ''}
    if caption:
        b['captions'] = [{'bbox': caption_box, 'content': caption}]
    return b


class GroupFiguresTests(unittest.TestCase):
    def group(self, blocks):
        return group_page({'page_idx': 0, 'blocks': blocks})

    def test_two_panels_share_one_caption(self):
        groups, diag = self.group([
            visual([.1,.1,.9,.3]),
            visual([.1,.32,.9,.6], 'Fig. 8 | A multi-panel diagram.', [.1,.62,.9,.68]),
        ])
        self.assertEqual([g['panel_count'] for g in groups], [2])
        self.assertFalse(groups[0]['review_required'])
        self.assertFalse(diag['unassigned_visuals'])

    def test_separate_stacked_figures(self):
        groups, _ = self.group([
            visual([.1,.1,.9,.3], 'Fig. 2 | First diagram.', [.1,.32,.9,.37]),
            visual([.1,.39,.9,.6], 'Fig. 3 | Second diagram.', [.1,.62,.9,.67]),
        ])
        self.assertEqual([g['panel_count'] for g in groups], [1,1])
        self.assertTrue(all(not g['review_required'] for g in groups))

    def test_nearby_side_by_side_figures_need_review(self):
        groups, _ = self.group([
            visual([.1,.1,.48,.4], 'Fig. 1 | Left figure.', [.1,.42,.48,.47]),
            visual([.51,.1,.9,.4], 'Fig. 2 | Right figure.', [.51,.42,.9,.47]),
        ])
        self.assertTrue(all(g['review_required'] for g in groups))
        self.assertEqual([g['panel_count'] for g in groups], [1,1])

    def test_two_column_caption_is_joined(self):
        groups, _ = self.group([
            visual([.1,.1,.9,.5], 'Fig. 5 | Caption starts here with a description.', [.1,.52,.48,.6]),
            {'type':'chart', 'bbox':[.7,.2,.8,.4], 'content':'', 'captions':[
                {'bbox':[.51,.52,.9,.59], 'content':'and continues in the right-hand column.'}]},
        ])
        self.assertIn('continues in the right-hand column', groups[0]['caption'])

    def test_body_reference_is_not_a_caption(self):
        groups, diag = self.group([
            {'type':'text', 'bbox':[.1,.1,.9,.2], 'content':'Figure 3b shows a difference in prediction results.'},
            visual([.1,.3,.9,.5]),
        ])
        self.assertFalse(groups)
        self.assertEqual(len(diag['unassigned_visuals']), 1)
        self.assertFalse(diag['unattached_caption_parts'])

    def test_body_text_separates_unrelated_visual(self):
        groups, diag = self.group([
            visual([.1,.1,.9,.3]),
            {'type':'text', 'bbox':[.1,.31,.9,.36], 'content':'This is an intervening paragraph of substantive body text.'},
            visual([.1,.37,.9,.6], 'Fig. 1 | Bottom figure.', [.1,.62,.9,.68]),
        ])
        self.assertEqual(groups[0]['panel_count'], 1)
        self.assertEqual(len(diag['unassigned_visuals']), 1)

    def test_above_figure_caption_is_reported(self):
        groups, diag = self.group([
            visual([.1,.3,.9,.6], 'Fig. 1 | Caption above the visual.', [.1,.2,.9,.25]),
        ])
        self.assertTrue(groups[0]['review_required'])
        self.assertIsNone(groups[0]['crop_bbox'])
        self.assertEqual(len(diag['unassigned_visuals']),1)


if __name__ == '__main__':
    unittest.main()
