from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TypeAlias


class SearchLanguageError(ValueError):
    """Raised when a search query is malformed or unsupported."""


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    position: int


@dataclass(frozen=True)
class Text:
    value: str
    phrase: bool = False


@dataclass(frozen=True)
class Field:
    name: str
    value: str


@dataclass(frozen=True)
class Not:
    operand: SearchNode


@dataclass(frozen=True)
class Boolean:
    operator: str
    left: SearchNode
    right: SearchNode


SearchNode: TypeAlias = Text | Field | Not | Boolean


def tokenize(query: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    while index < len(query):
        if query[index].isspace():
            index += 1
            continue

        position = index
        character = query[index]
        if character == "(":
            tokens.append(Token("LPAREN", character, position))
            index += 1
        elif character == ")":
            tokens.append(Token("RPAREN", character, position))
            index += 1
        elif character == ":":
            tokens.append(Token("COLON", character, position))
            index += 1
        elif character == '"':
            index += 1
            value: list[str] = []
            while index < len(query) and query[index] != '"':
                if query[index] == "\\" and index + 1 < len(query) and query[index + 1] in {'"', "\\"}:
                    index += 1
                value.append(query[index])
                index += 1
            if index == len(query):
                raise SearchLanguageError(f"Unterminated quoted phrase at position {position}")
            if not value:
                raise SearchLanguageError(f"Quoted phrases cannot be empty at position {position}")
            tokens.append(Token("PHRASE", "".join(value), position))
            index += 1
        else:
            while index < len(query) and not query[index].isspace() and query[index] not in '():"':
                index += 1
            value = query[position:index]
            operator = value.upper()
            kind = operator if operator in {"AND", "OR", "NOT"} else "WORD"
            tokens.append(Token(kind, value, position))

    return tokens


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    def parse(self) -> SearchNode:
        if not self.tokens:
            raise SearchLanguageError("Query cannot be empty")
        expression = self._parse_or()
        if self.index < len(self.tokens):
            token = self.tokens[self.index]
            raise SearchLanguageError(f"Unexpected token {token.value!r} at position {token.position}")
        return expression

    def _parse_or(self) -> SearchNode:
        expression = self._parse_and()
        while self._accept("OR"):
            expression = Boolean("OR", expression, self._parse_and())
        return expression

    def _parse_and(self) -> SearchNode:
        expression = self._parse_not()
        while self.index < len(self.tokens):
            if self._accept("AND"):
                expression = Boolean("AND", expression, self._parse_not())
            elif self._starts_expression(self.tokens[self.index]):
                expression = Boolean("AND", expression, self._parse_not())
            else:
                break
        return expression

    def _parse_not(self) -> SearchNode:
        if self._accept("NOT"):
            return Not(self._parse_not())
        return self._parse_primary()

    def _parse_primary(self) -> SearchNode:
        if self._accept("LPAREN"):
            expression = self._parse_or()
            if not self._accept("RPAREN"):
                token = self.tokens[self.index] if self.index < len(self.tokens) else None
                position = token.position if token else (self.tokens[-1].position + len(self.tokens[-1].value))
                raise SearchLanguageError(f"Expected ')' at position {position}")
            return expression

        if self.index >= len(self.tokens):
            raise SearchLanguageError("Expected a search term")

        token = self.tokens[self.index]
        if token.kind == "WORD" and self.index + 1 < len(self.tokens) and self.tokens[self.index + 1].kind == "COLON":
            name = token.value.casefold()
            self.index += 2
            if self.index >= len(self.tokens) or self.tokens[self.index].kind not in {"WORD", "PHRASE"}:
                raise SearchLanguageError(f"Expected a value for field {name!r} at position {token.position}")
            value = self.tokens[self.index].value
            self.index += 1
            return Field(name, value)

        if token.kind in {"WORD", "PHRASE"}:
            self.index += 1
            return Text(token.value, phrase=token.kind == "PHRASE")

        raise SearchLanguageError(f"Expected a search term at position {token.position}")

    def _accept(self, kind: str) -> bool:
        if self.index < len(self.tokens) and self.tokens[self.index].kind == kind:
            self.index += 1
            return True
        return False

    @staticmethod
    def _starts_expression(token: Token) -> bool:
        return token.kind in {"WORD", "PHRASE", "LPAREN", "NOT"}


def parse_query(query: str) -> SearchNode:
    return _Parser(tokenize(query)).parse()


@dataclass(frozen=True)
class CompiledQuery:
    where_sql: str
    parameters: tuple[str, ...]


class SearchCompiler:
    """Compile parsed searches for message alias ``m`` and FTS table ``message_fts``."""

    def compile(self, expression: SearchNode) -> CompiledQuery:
        parameters: list[str] = []
        where_sql = self._compile_node(expression, parameters)
        return CompiledQuery(where_sql, tuple(parameters))

    def _compile_node(self, expression: SearchNode, parameters: list[str]) -> str:
        if isinstance(expression, Text):
            phrase = expression.value.replace('"', '""')
            parameters.append(f'"{phrase}"')
            return (
                "EXISTS (SELECT 1 FROM message_fts "
                "WHERE message_fts.rowid = m.message_observation_id AND message_fts MATCH ?)"
            )

        if isinstance(expression, Field):
            value = self._compile_field(expression)
            parameters.extend(value[1])
            return value[0]

        if isinstance(expression, Not):
            return f"NOT ({self._compile_node(expression.operand, parameters)})"

        if expression.operator not in {"AND", "OR"}:
            raise SearchLanguageError(f"Unsupported boolean operator: {expression.operator!r}")
        left_sql = self._compile_node(expression.left, parameters)
        right_sql = self._compile_node(expression.right, parameters)
        return f"({left_sql} {expression.operator} {right_sql})"

    @staticmethod
    def _compile_field(expression: Field) -> tuple[str, tuple[str, ...]]:
        name = expression.name
        if name == "character":
            return (
                "EXISTS (SELECT 1 FROM message_attributions a "
                "JOIN characters ch ON ch.character_id = a.character_id "
                "WHERE a.message_observation_id = m.message_observation_id "
                "AND a.status = 'accepted' AND ch.character_name = ? COLLATE NOCASE)",
                (expression.value,),
            )
        if name == "player":
            return (
                "EXISTS (SELECT 1 FROM message_attributions a "
                "JOIN players p ON p.player_id = a.player_id "
                "WHERE a.message_observation_id = m.message_observation_id "
                "AND a.status = 'accepted' AND p.player_name = ? COLLATE NOCASE)",
                (expression.value,),
            )
        if name == "channel":
            return (
                "EXISTS (SELECT 1 FROM channels c WHERE c.channel_id = m.channel_id "
                "AND c.name_current = ? COLLATE NOCASE)",
                (expression.value,),
            )
        if name == "after":
            try:
                parsed_date = date.fromisoformat(expression.value)
            except ValueError as error:
                raise SearchLanguageError(f"Invalid date for after: {expression.value!r}") from error
            if parsed_date.isoformat() != expression.value:
                raise SearchLanguageError(f"Date must use YYYY-MM-DD format: {expression.value!r}")
            return "m.timestamp_utc >= ?", (f"{parsed_date.isoformat()}T00:00:00+00:00",)
        raise SearchLanguageError(f"Unsupported search field: {name!r}")