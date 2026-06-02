"""
hello.py - A simple greeting module.

Provides a function to print a greeting message to the console.
"""


def greet(name: str = "World") -> str:
    """Generate a greeting string.

    Args:
        name: The name to greet. Defaults to "World".

    Returns:
        A formatted greeting string, e.g. "Hello, World!".
    """
    return f"Hello, {name}!"


def main() -> None:
    """Main entry point: print the default greeting."""
    message: str = greet()
    print(message)


if __name__ == "__main__":
    main()
