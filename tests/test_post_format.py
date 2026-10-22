import unittest
from app.post_format import parse_post,safe_url


class PostTests(unittest.TestCase):
    def test_formats_and_entities(self):
        html,text,title=parse_post('<strong>A &amp; B</strong>\n<em>C</em><u>D</u><s>E</s><a href="https://example.com?a=1&amp;b=2">link</a>')
        self.assertEqual(title,'A & B');self.assertIn('<b>A &amp; B</b>',html)
        self.assertIn('a=1&amp;b=2',html)
        self.assertEqual(text,'A & B\nCDElink')
        self.assertEqual(parse_post('<b onclick="bad()">Safe</b>')[0],'<b>Safe</b>')
    def test_rejects_unsafe_and_invalid_posts(self):
        for value in ('','<script>bad</script>','<img src=x onerror=bad()>','<a href="javascript:alert(1)">x</a>','<b>x</i>','<b>x','<code><b>x</b></code>','<blockquote><blockquote>x</blockquote></blockquote>','😀'*1501):
            with self.subTest(value=value[:40]),self.assertRaises(ValueError):parse_post(value)
        for value in ('https://example.com/channel/123','https://t.me/hackathons','javascript:bad','https://t.me/share/url'):
            with self.subTest(value=value),self.assertRaises(ValueError):safe_url(value,channel=True)
        self.assertEqual(safe_url('https://t.me/c/123456/8',channel=True),'https://t.me/c/123456/8')
