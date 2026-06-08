from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, TypedDict

import asyncpg
import typer

from migratory.utils import run_async


if TYPE_CHECKING:
    class MigratoryConfig(TypedDict):
        postgres_uri: str
        applied: List[str]


class Revision:
    def __init__(self, file: Path) -> None:
        self.file = file

    async def forward(self, conn: asyncpg.Connection):
        sql = self.file.read_text("utf-8")
        await conn.execute(sql)

    def __str__(self) -> str:
        return str(self.file)


class PYRevision(Revision):
    """A .py revision file"""
    def __init__(self, file: Path) -> None:
        super().__init__(file)
        spec = importlib.util.spec_from_file_location(str(file), file)

        if not spec:
            raise Exception("Missing spec")

        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    async def forward(self, conn: asyncpg.Connection):
        await self.module.start(conn)


class Migrator:
    """The main class for Migratory."""
    __slots__ = ("root", "revisions", "config", "root_config")

    def __init__(self) -> None:
        self.root = Path("migrations")
        self.revisions = self.load_migrations()
        self.config = self.load_config()
        self.root_config = Path("migratory.json")

    def load_migrations(self):
        fps: List[Revision] = []
        for file in self.root.glob("*.sql"):
            fps.append(Revision(file))

        for file in self.root.glob("*.py"):
            fps.append(PYRevision(file))
        return fps

    @property
    def sorted_revisions(self):
        return sorted(self.revisions, key=lambda x: str(x.file))

    def load_config(self) -> MigratoryConfig:
        try:
            with self.root_config.open(mode="r", encoding="utf-8") as fp:
                return json.load(fp)
        except FileNotFoundError:
            raise Exception("Configuration file not found! Please run `migratory init` first.")

    def save(self):
        with self.root_config.open(mode="w", encoding="utf-8") as fp:
            json.dump(self.config, fp, sort_keys=True)

    async def apply_revisions(self, conn: asyncpg.Connection):
        revisions = self.sorted_revisions
        applied: List[Revision] = []
        async with conn.transaction():
            for revision in revisions:
                if str(revision) in self.config['applied']:
                    continue

                await revision.forward(conn)
                typer.secho(f"Applied {str(revision)}!", fg=typer.colors.GREEN)
                applied.append(revision)

        self.config['applied'].extend([str(a) for a in applied])
        self.save()

        return applied

    def display_pending_revisions(self):
        for rev in self.sorted_revisions:
            if rev in self.config['applied']:
                continue
            sql = rev.file.read_text("utf-8")
            typer.echo(f"{str(rev)}\n{sql}")


parser = typer.Typer(name='migratory', help="Database migration commands",
                     no_args_is_help=True)


@parser.command()
def init():
    """Initializes migratory for your project."""
    root = Path("migrations/")
    if root.exists():
        typer.echo("✅ Migrations directory already exists!")
    else:
        typer.echo("🔄 Initializing migrations directory...")
        root.mkdir(exist_ok=False)
        typer.echo("✅ Created migrations directory!")

    typer.echo("✅  Initialized migrations directory!")
    typer.echo("You can now add .sql or .py files to the migrations directory "
               "and run `migratory upgrade` to apply them! Naming convention "
               "requires that use a format of \"0001_{description}.sql\"")

    # Ask for connection uri
    prompt = typer.prompt("Please enter your PostgreSQL uri (e.g. postgres://user:password@localhost:5432/dbname)")
    if not prompt:
        typer.echo("PostgreSQL URI is required! Please run `migratory init` again and provide a valid URI.")
        return

    config = {"postgres_uri": prompt, "applied": []}
    parent = root.parent
    (parent / "migratory.json").write_text(json.dumps(config, indent=4))
    typer.echo("✅ Saved PostgreSQL URI to migratory.json!")

    # If a .gitignore file exists, add the configuration file to it
    gitignore = Path(".gitignore")
    if gitignore.exists():
        content = gitignore.read_text("utf-8")
        if "migratory.json" not in content:
            gitignore.write_text(content + "\nmigratory.json\n", encoding="utf-8")
            typer.echo("✅ Added migratory.json to .gitignore!")

    typer.echo("Migratory has been successfully setup!")


@parser.command()
def new(name: str = typer.Argument(..., help="The name of the migration")):
    """Creates a new migration file with the given name."""
    try:
        m = Migrator()
    except Exception as e:
        typer.echo("Error occurred while reading the configuration file!\n"
                   "Please make sure you ran `migratory init` first and "
                   "that your migratory.json file is valid.\n")
        raise e

    num = len(m.revisions) + 1
    filename = f"{num:04d}_{name}.sql"
    path = m.root / filename
    if path.exists():
        typer.echo("A migration with that name already exists!")
        return

    path.write_text("-- Write your SQL migration below here!\n",
                    encoding="utf-8")
    typer.echo(f"Created new migration at {str(path)}")


@parser.command()
@run_async
async def upgrade(sql: Optional[bool] = typer.Option(False,
                                                     help="Displays the SQL that would be applied", is_flag=True)):
    """Applies all pending migrations"""
    m = Migrator()

    if sql:
        m.display_pending_revisions()
        return

    try:
        conn = await asyncpg.connect(m.config["postgres_uri"])
    except Exception as e:
        typer.echo(f"Unable to connect to the database!\n{e}")
        raise e

    applied = await m.apply_revisions(conn)
    await conn.close()
    typer.echo(f"Applied {len(applied)} migrations!")


@parser.command(name='log')
def display_log():
    """Displays what migrations are pending and are applied"""
    m = Migrator()

    for rev in m.sorted_revisions:
        if str(rev) in m.config['applied']:
            style = typer.style("Applied", fg='green')
        else:
            style = typer.style("Pending", fg='red')

        typer.echo(f"{style} {str(rev)}")


@parser.command('reset')
@run_async
async def reset_migrations():
    """Resets your applied migrations list"""
    m = Migrator()

    if m.config["applied"] == []:
        typer.echo("No migrations have been applied yet!")
        return

    m.config["applied"] = []
    m.save()
    typer.echo("Reset your migrations config")


@parser.command('destroy')
def destroy():
    """Destroys your migratory configuration and migrations directory"""
    root = Path("migrations/")
    if root.exists():
        for file in root.glob("*"):
            file.unlink()
        root.rmdir()
        typer.echo("Destroyed migrations directory and all migration files!")
    else:
        typer.echo("No migrations directory found!")

    config = Path("migratory.json")
    if config.exists():
        config.unlink()
        typer.echo("Destroyed migratory.json configuration file!")
    else:
        typer.echo("No migratory.json configuration file found!")

