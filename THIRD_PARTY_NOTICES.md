# Third-party notices

The release container is based on Debian and installs packages from Debian's
official repositories. Their copyright and license texts are available below
`/usr/share/doc` in the image and from the Debian package metadata.

## HandBrake

HandBrake is Copyright © The HandBrake Team and contributors and is distributed
under the GNU General Public License version 2.

- Project: https://github.com/HandBrake/HandBrake
- License: https://github.com/HandBrake/HandBrake/blob/master/LICENSE
- Corresponding source used by this image:
  `https://github.com/HandBrake/HandBrake/releases/download/1.11.2/HandBrake-1.11.2-source.tar.bz2`

`Dockerfile.release` documents the complete download, checksum verification, and
build process used for the binary shipped in the public container image.

## FFmpeg and codecs

The image uses the FFmpeg and codec packages distributed by Debian. Their exact
versions and applicable licenses depend on the Debian release used at build time.
Users distributing derived images are responsible for reviewing package licenses
and patent obligations applicable in their jurisdiction.
