"""PDF-only prefilter coverage against the pre-optimization detector semantics."""
from io import BytesIO
import unittest
from unittest.mock import patch

from pypdf import PdfWriter
from providers.jp.jpx_manual import historical_direct as direct


def response(body, *, status=200, mime='application/pdf'):
    return direct.Response('https://www.jpx.co.jp/test.pdf', status,
                           {'Content-Type': mime}, body)


def full_inspection(value):
    """Frozen prefilter-free _challenge decision tree for semantic comparisons."""
    text = value.body.decode('utf-8', errors='replace')
    controls = direct.ChallengeControls()
    controls.feed(text)
    controls.close()
    if controls.found or direct.CHALLENGE.fullmatch(text.strip()):
        return True
    if value.status != 200:
        return bool(direct.CHALLENGE.search(text))
    if value.content_type() != 'text/html' and not value.body.startswith(b'%PDF-'):
        return bool(direct.CHALLENGE.search(text))
    if value.body.startswith(b'%PDF-') and direct.CHALLENGE.search(text):
        try:
            reader = direct.PdfReader(BytesIO(value.body), strict=True)
            return reader.is_encrypted or not len(reader.pages)
        except Exception:
            return True
    return False


class PDFSecurityPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        output = BytesIO()
        writer.write(output)
        cls.valid_pdf = output.getvalue()

    def assert_fallback(self, body, expected, **options):
        value = response(body, **options)
        self.assertEqual(full_inspection(value), expected)
        original = direct.ChallengeControls.feed
        with patch.object(direct.ChallengeControls, 'feed', autospec=True,
                          side_effect=original) as feed:
            self.assertEqual(direct._challenge(value), expected)
            self.assertGreaterEqual(feed.call_count, 1)

    def test_p1_large_binary_noise_skips_full_html_parser(self):
        # NUL terminates the tag-name fragments; there are no semantic h1/h2
        # boundaries. Millions of angle brackets must not build parser stacks.
        body = b'%PDF-' + (b'< > <h1\x00 <h2\x00 ' * 400_000) + b'BOt'
        self.assertGreater(len(body), 5_000_000)
        with patch.object(direct.ChallengeControls, 'feed',
                          side_effect=AssertionError('FULL_HTML_PARSER_CALLED')) as feed:
            self.assertFalse(direct._challenge(response(body)))
        feed.assert_not_called()

    def test_p2_large_body_late_recaptcha_falls_back(self):
        body = b'%PDF-' + b'\x00' * 5_000_000 + b'<div class="g-recaptcha"></div>'
        self.assert_fallback(body, True)

    def test_p3_large_body_late_turnstile_falls_back(self):
        body = b'%PDF-' + b'\x00' * 5_000_000 + b'<div class="cf-turnstile"></div>'
        self.assert_fallback(body, True)

    def test_p4_entity_obfuscated_control_on_arbitrary_tag(self):
        for fragment in (
            b'<div class="g-re&#99;aptcha"></div>',
            b'<x class="&#103;&#45;&#114;&#101;&#99;&#97;&#112;&#116;&#99;&#104;&#97;">',
            b'<x class=g-re&#99aptcha>',  # HTMLParser accepts missing semicolon.
            b'<x class="g-re&unknown;captcha" class="g-re&#99;aptcha">',
            b'<x data="< >"class="g-re&#99;aptcha">',  # Adjacent attributes.
            b'<x\xff class="g-re&#99;aptcha">',  # Same replacement decoding.
            b'<x/class="g-re&#99;aptcha">',
        ):
            with self.subTest(fragment=fragment):
                self.assert_fallback(b'%PDF-' + fragment, True)

    def test_p5_ordinary_documentation_remains_false(self):
        for fragment in (
            b'<title>CAPTCHA security documentation</title>',
            b'<h1>CAPTCHA explanation</h1><button>Continue reading</button>',
            b'<div class="g-recaptcha-documentation">CAPTCHA support guide.</div>',
            b'<script>// Documentation: grecaptcha.render is a method.</script>',
            b'<x class="captcha-explanation">CAPTCHA support information</x>',
        ):
            with self.subTest(fragment=fragment):
                self.assert_fallback(self.valid_pdf + fragment, False)

    def test_all_widget_and_captcha_tokens_preserve_detection(self):
        for token in direct.ChallengeControls.WIDGET_TOKENS | direct.ChallengeControls.CAPTCHA_CONTROLS:
            with self.subTest(token=token):
                self.assert_fallback(b'%PDF-<div class="' + token.encode() + b'">', True)

    def test_all_generic_controls_preserve_detection(self):
        for token in direct.ChallengeControls.GENERIC_TOKENS:
            with self.subTest(token=token):
                self.assert_fallback(('%PDF-<button class="' + token + '">Verify</button>').encode(), True)

    def test_provider_and_control_endpoint_matrix(self):
        for fragment in (
            '<iframe src="/recaptcha/api2/anchor">',
            '<iframe src="/recaptcha/enterprise/anchor">',
            '<script src="https://hcaptcha.com/1/api.js">',
            '<script src="https://js.hcaptcha.com/api.js">',
            '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js">',
            '<form action="/captcha">', '<form action="/recaptcha?x=1">',
            '<input src="/cf-browser-verification/">',
            '<form action="/verıfication/">',  # re.I Unicode equivalence.
            *('<form action="/' + token + '/">'
              for token in direct.ChallengeControls.GENERIC_TOKENS),
        ):
            with self.subTest(fragment=fragment):
                self.assert_fallback(('%PDF-' + fragment).encode(), True)

    def test_challenge_prompt_matrix_including_japanese_unicode(self):
        for prompt in (
            'CAPTCHA', 'Access denied', 'Security challenge', 'Verify human',
            'Checking your browser', 'Just a moment', 'Cloudflare Ray',
            'Bot challenge', 'Auth required', 'Authentication challenge',
            'Login required', 'Sign-in required', 'Sign in to continue',
            'アクセス拒否', 'アクセス制限', 'ロボットではない', 'セキュリティ確認',
            'Verİfy human', 'ſign in required',
        ):
            with self.subTest(prompt=prompt):
                self.assert_fallback(('%PDF-<title>' + prompt + '</title>').encode(), True)

    def test_fragmented_prompts_and_encoded_text_cannot_skip(self):
        for fragment in (
            b'<h1>ca<x>ptcha</x></h1>',
            b'<h1 data="< >">ca<x>ptcha</x></h1>',
            b'<title>&#99;&#97;&#112;&#116;&#99;&#104;&#97;</title>',
            b'<div role="alert">a<x>ccess</x> denied</div>'
            b'<h2>Verification required</h2><button>Continue</button>',
            b'<div role="alert">Access restricted</div>'
            b'<button><x>Ver</x>ify</button>',
        ):
            with self.subTest(fragment=fragment):
                self.assert_fallback(b'%PDF-' + fragment, True)

    def test_inline_script_and_document_write_preserve_detection(self):
        for fragment in (
            b'<script>grecaptcha.render("x", {});</script>',
            b'<script>hcaptcha.execute();</script>',
            b'<script>turnstile.render("x", {});</script>',
            b'''<script>document.write('<x class="g-re&#99;aptcha">');</script>''',
        ):
            with self.subTest(fragment=fragment):
                self.assert_fallback(b'%PDF-' + fragment, True)

    def test_non_pdf_and_html_mime_still_use_full_inspection(self):
        for body, mime in ((b'<h1>CAPTCHA</h1>', 'text/html'),
                           (b'Ordinary listing prose', 'text/html'),
                           (b'Not PDF magic', 'application/pdf')):
            value = response(body, mime=mime)
            self.assert_fallback(body, full_inspection(value), mime=mime)

    def test_standalone_bot_is_not_a_challenge_signal(self):
        value = response(b'%PDF-BOt')
        self.assertFalse(full_inspection(value))
        with patch.object(direct.ChallengeControls, 'feed') as feed:
            self.assertFalse(direct._challenge(value))
        feed.assert_not_called()

    def test_malformed_attribute_keeps_existing_false_result(self):
        # The existing HTMLParser does not recognize this value as a widget.
        # Ambiguous syntax still falls back; the prefilter must not classify it.
        self.assert_fallback(
            b'%PDF-<x class=== "&#103;&#45;&#114;&#101;&#99;&#97;&#112;&#116;&#99;&#104;&#97;">',
            False)

    def test_pdf_status_and_mime_do_not_hide_challenge(self):
        for status in (200, 403, 429, 500):
            for mime in ('application/pdf', 'text/html', 'application/octet-stream'):
                with self.subTest(status=status, mime=mime):
                    self.assert_fallback(b'%PDF-<title>CAPTCHA</title>', True,
                                         status=status, mime=mime)

    def test_security_prefilter_does_not_replace_pdf_validity_checks(self):
        for body in (b'%PDF-not-a-real-pdf', b'%PDF-'):
            value = response(body)
            self.assertFalse(direct._challenge(value))
            with self.assertRaisesRegex(ValueError, 'PDF_CORRUPT'):
                direct.validate_pdf(value)
        output = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt('password')
        writer.write(output)
        value = response(output.getvalue())
        with self.assertRaisesRegex(ValueError, 'PDF_CORRUPT'):
            direct.validate_pdf(value)


if __name__ == '__main__':
    unittest.main()
