"""Self-update over GitHub Releases.

    release.py    what a release is, and the version comparison
    feed.py       the network: the Releases API and the asset download
    client.py     check for a newer build, work out what kind of install this
                  is, fetch the installer and verify it
    installer.py  run it, with Inno Setup's own silent switches

THERE IS NO swap.py ANY MORE. Up to 1.9.0 an update was a batch file this
package wrote and ran, which replaced the exe underneath the app. It worked,
and it read to a behavioural antivirus engine as a dropper, because that is
what those steps are when you list them out. From 2.0.0 an installed copy is
updated by the installer that made it, and a portable copy is not updated at
all: it is told to download the new zip. installer.py's docstring has the
whole argument.

Qt-free on purpose. The UI half is `ui/update_notice.py`, which knows about
threads and buttons and nothing about versions, hashes or command lines.
"""
