import sys

import click

from .image_build import build_image


@click.group(name="image")
def image():
    """Build a custom Warnet Bitcoin Core image"""


@image.command()
@click.option("--repo", required=True, type=str)
@click.option("--commit-sha", required=True, type=str)
@click.option(
    "--tags",
    required=True,
    type=str,
    help="Comma-separated list of full tags including image names",
)
@click.option("--build-args", required=False, type=str)
@click.option("--arches", required=False, type=str)
@click.option("--action", required=False, type=str, default="load")
@click.option(
    "--repo-url",
    required=False,
    type=str,
    default="",
    help="Optional full git remote URL. Useful with --ssh for private repositories.",
)
@click.option(
    "--ssh",
    is_flag=True,
    help="Forward the default SSH agent into the Docker build for private git repositories.",
)
@click.option(
    "--build-jobs",
    required=False,
    type=str,
    default="",
    help="Optional CMake build parallelism, for example 4 on memory-limited Docker Desktop.",
)
def build(repo, commit_sha, tags, build_args, arches, action, repo_url, ssh, build_jobs):
    """Build a Bitcoin Core Docker image with specified parameters from specified commit or tag.

    \b
    Usage Examples:
        # Build an image for Warnet repository
            warnet image build --repo bitcoin/bitcoin --commit-sha d6db87165c6dc2123a759c79ec236ea1ed90c0e3 --tags bitcoindevproject/bitcoin:v29.0-rc2 --action push
        # Build from a tag instead of commit hash
            warnet image build --repo bitcoin/bitcoin --commit-sha v31.0 --tags bitcoindevproject/bitcoin:v31.0 --action push
        # Build an image for local testing on arm64 only
            warnet image build --repo bitcoin/bitcoin --commit-sha d6db87165c6dc2123a759c79ec236ea1ed90c0e3 --tags bitcoindevproject/bitcoin:v29.0-rc2 --arches arm64 --action load
        # Build a private GitHub repository using your SSH agent
            warnet image build --repo owner/repo --commit-sha d6db87165c6dc2123a759c79ec236ea1ed90c0e3 --tags owner/repo:warnet --arches arm64 --action load --ssh --build-jobs 4
    """
    res = build_image(repo, commit_sha, tags, build_args, arches, action, repo_url, ssh, build_jobs)
    if not res:
        sys.exit(1)
