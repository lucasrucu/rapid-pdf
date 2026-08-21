"""Self-update over GitHub Releases.

    release.py  what a release is, and the version comparison
    feed.py     the network: the Releases API and the asset download
    client.py   check for a newer build, fetch it, verify it, stage it
    swap.py     the helper that replaces the exe once the app has exited

Qt-free on purpose. The UI half is `ui/update_notice.py`, which knows about
threads and buttons and nothing about versions, hashes or batch files.
"""
