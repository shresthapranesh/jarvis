"""Print the GraphQL SDL to stdout.

Usage:
    uv run python -m server.graphql.export_schema > schema.graphql
"""

from __future__ import annotations

from .schema import schema


def main() -> None:
    print(schema.as_str())


if __name__ == "__main__":
    main()
