"""Fetch only the permitted, pinned sqlglot source archive; verify its digest."""
import hashlib
import io
from pathlib import Path, PurePosixPath
import sys
import tarfile
import urllib.request

COMMIT = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
SHA256 = '93b2445f15f395b81c20556777ece19a6c7507df950f13fa9fac4a3be739af0d'
URL = f'https://codeload.github.com/tobymao/sqlglot/tar.gz/{COMMIT}'


def main():
    destination = Path(sys.argv[1])
    with urllib.request.urlopen(URL, timeout=90) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != SHA256:
        raise SystemExit('Pinned source archive checksum mismatch')
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as source:
        for member in source:
            parts = PurePosixPath(member.name).parts[1:]
            # The pinned archive includes CLAUDE.md -> AGENTS.md. Analysis
            # does not need documentation symlinks; never create archive links.
            if not parts or member.isdir() or member.issym() or member.islnk():
                continue
            if not member.isfile() or '..' in parts:
                raise SystemExit('Unexpected archive member')
            # Analysis needs Python source and fixture paths, not the .git history.
            path = destination.joinpath(*parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as content:
                path.write_bytes(content.read())


if __name__ == '__main__':
    main()
