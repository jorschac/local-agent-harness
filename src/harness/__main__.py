"""Run the HTTP CLI with ``python -m harness``; harnessd is server.main."""
from .cli import app

if __name__ == "__main__":
    app()
