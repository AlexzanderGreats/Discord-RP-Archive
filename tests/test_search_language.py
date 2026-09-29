from __future__ import annotations

import unittest

from archive.search_language import (
    Boolean,
    Field,
    SearchCompiler,
    SearchLanguageError,
    Text,
    parse_query,
)


class SearchLanguageTests(unittest.TestCase):
    def test_parses_plain_term_and_exact_phrase(self) -> None:
        self.assertEqual(parse_query("Winter"), Text("Winter"))
        self.assertEqual(parse_query('"God Shattering Star"'), Text("God Shattering Star", phrase=True))

    def test_boolean_precedence_and_parentheses(self) -> None:
        self.assertEqual(
            parse_query("Winter OR Ultima AND Oblivion"),
            Boolean("OR", Text("Winter"), Boolean("AND", Text("Ultima"), Text("Oblivion"))),
        )
        self.assertEqual(
            parse_query("(Winter OR Ultima) AND Oblivion"),
            Boolean(
                "AND",
                Boolean("OR", Text("Winter"), Text("Ultima")),
                Text("Oblivion"),
            ),
        )

    def test_parses_fields_and_implicit_and(self) -> None:
        self.assertEqual(parse_query('character:Winter AND "silver compass"'),
                         Boolean("AND", Field("character", "Winter"), Text("silver compass", phrase=True)))
        self.assertEqual(parse_query("after:2022-06-01 channel:marketplace"),
                         Boolean("AND", Field("after", "2022-06-01"), Field("channel", "marketplace")))

    def test_compiles_fields_and_text_to_parameterized_predicates(self) -> None:
        compiled = SearchCompiler().compile(parse_query('character:Winter AND "silver compass"'))

        self.assertIn("message_attributions", compiled.where_sql)
        self.assertIn("message_fts MATCH ?", compiled.where_sql)
        self.assertEqual(compiled.parameters, ("Winter", '"silver compass"'))

        player = SearchCompiler().compile(parse_query('player:"Player A"'))
        self.assertIn("JOIN players p", player.where_sql)
        self.assertEqual(player.parameters, ("Player A",))

    def test_compiler_binds_untrusted_values(self) -> None:
        compiled = SearchCompiler().compile(parse_query('channel:"x\' OR 1=1 --"'))

        self.assertNotIn("x' OR 1=1", compiled.where_sql)
        self.assertEqual(compiled.parameters, ("x' OR 1=1 --",))

    def test_compiles_after_as_utc_day_boundary(self) -> None:
        compiled = SearchCompiler().compile(parse_query("after:2022-06-01"))

        self.assertEqual(compiled.where_sql, "m.timestamp_utc >= ?")
        self.assertEqual(compiled.parameters, ("2022-06-01T00:00:00+00:00",))

    def test_rejects_unsupported_fields_invalid_dates_and_malformed_queries(self) -> None:
        with self.assertRaises(SearchLanguageError):
            SearchCompiler().compile(parse_query("server:Archive"))
        with self.assertRaises(SearchLanguageError):
            SearchCompiler().compile(parse_query("after:2022-02-30"))
        with self.assertRaises(SearchLanguageError):
            SearchCompiler().compile(Boolean("UNION SELECT", Text("a"), Text("b")))
        with self.assertRaises(SearchLanguageError):
            parse_query('"unfinished')
        with self.assertRaises(SearchLanguageError):
            parse_query("Winter AND")


if __name__ == "__main__":
    unittest.main()