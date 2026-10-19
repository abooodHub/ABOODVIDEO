"""Administrative CLI Commands."""
import sys
import click
from app.core.database import SyncSessionLocal
from app.models.user import User, UserRole
from app.repositories.user_repo import SyncUserRepository
from app.security.hashing import hash_password
from app.core.logging import logger


@click.group()
def cli():
    """ABOODVIDEO Management CLI."""
    pass


@cli.command("create-owner")
@click.option("--email", prompt="Owner Email", help="Email address for the Owner user")
@click.option("--username", prompt="Owner Username", help="Username for the Owner user")
@click.option(
    "--password",
    prompt=True,
    hide_input=True,
    confirmation_prompt=True,
    help="Secure password for the Owner user",
)
def create_owner(email: str, username: str, password: str):
    """
    Rule 7: Secure CLI command to create the initial OWNER idempotently.
    Prevents automatic creation on startup and ensures strict password validation.
    """
    email = email.strip().lower()
    username = username.strip()

    if len(password) < 8:
        click.echo("Error: Password must be at least 8 characters long.", err=True)
        sys.exit(1)

    with SyncSessionLocal() as session:
        # Check existing user
        existing_email = SyncUserRepository.get_by_email(session, email)
        if existing_email:
            click.echo(f"Error: User with email '{email}' already exists.", err=True)
            sys.exit(1)

        existing_user = SyncUserRepository.get_by_username(session, username)
        if existing_user:
            click.echo(f"Error: Username '{username}' is already taken.", err=True)
            sys.exit(1)

        # Hash with Argon2id
        hashed = hash_password(password)

        owner = User(
            email=email,
            username=username,
            hashed_password=hashed,
            role=UserRole.OWNER,
            is_active=True,
        )

        SyncUserRepository.create(session, owner)
        click.echo(f"Successfully created initial OWNER account: '{username}' ({email})")


@cli.command("cleanup-uploads")
def cleanup_uploads():
    """Prune expired upload sessions and orphaned chunk directories."""
    import asyncio
    from app.core.database import AsyncSessionLocal
    from app.services.upload_service import UploadService

    async def _run():
        async with AsyncSessionLocal() as session:
            count = await UploadService.cleanup_expired_sessions(session)
            click.echo(f"Successfully cleaned up {count} expired upload sessions.")

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
