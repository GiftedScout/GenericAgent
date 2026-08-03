import unittest

from frontends.terminal_math import adapt_math_markdown, latex_to_unicode


class TerminalMathTests(unittest.TestCase):
    def test_event_likelihood_formula(self):
        source = (r"\log \mathcal L_{\rm events} = \sum_i \left[ "
                  r"\operatorname{logsumexp}_j(\log w_{ij}) - \log N_i \right]")
        self.assertEqual(
            latex_to_unicode(source),
            "log ℒₑᵥₑₙₜₛ = ∑ᵢ [ logsumexpⱼ(log wᵢⱼ) - log Nᵢ ]",
        )

    def test_fraction_integral_and_relations(self):
        self.assertEqual(
            latex_to_unicode(r"\frac{N_{\rm production}}{\max_k \tau_k} \ge 50"),
            "(N₍production₎)⁄(maxₖ τₖ) ≥ 50",
        )
        self.assertEqual(latex_to_unicode(r"\int_0^\infty p(x)\,dx"), "∫₀^∞ p(x) dx")

    def test_display_delimiters_become_text_fences(self):
        for markdown in (r"\[H_0 = 70\]", "$$H_0 = 70$$"):
            with self.subTest(markdown=markdown):
                converted = adapt_math_markdown(markdown)
                self.assertIn("```text\nH₀ = 70\n```", converted)
                self.assertNotIn("H_0", converted)

    def test_inline_delimiters_become_inline_code(self):
        self.assertEqual(adapt_math_markdown(r"A \(H_0=70\) B"), "A `H₀=70` B")
        self.assertEqual(adapt_math_markdown("A $H_0=70$ B"), "A `H₀=70` B")

    def test_currency_is_not_treated_as_math(self):
        text = "It costs $20 and the other costs $30."
        self.assertEqual(adapt_math_markdown(text), text)

    def test_existing_code_is_unchanged(self):
        samples = [
            '`$H_0$` and $H_0$',
            '```python\nformula = r"$$H_0$$"\n```\n$$H_0$$',
            '    formula = r"$$H_0$$"\n\n$$H_0$$',
        ]
        for markdown in samples:
            with self.subTest(markdown=markdown):
                converted = adapt_math_markdown(markdown)
                self.assertIn('H_0', converted)
                self.assertIn('H₀', converted)

    def test_plain_markdown_and_unknown_commands_are_lossless(self):
        plain = "# Heading\n\nNo mathematics here."
        self.assertIs(adapt_math_markdown(plain), plain)
        self.assertEqual(latex_to_unicode(r"\unknown{x}"), r"\unknownx")


if __name__ == "__main__":
    unittest.main()
