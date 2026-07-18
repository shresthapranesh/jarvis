"""Print the GraphQL SDL to stdout."""

from __future__ import annotations

from .schema import schema


def main() -> None:
    print(schema.as_str())


if __name__ == "__main__":
    main()
