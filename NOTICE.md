# Distribution scope

The MIT license covers the Setforge source code and documentation in this
repository. It does not grant rights to user-supplied references, generated
images, characters, trademarks or third-party services. No card art, complete
reference screenshots, Miro board export or screenshot-derived font atlas is included.

The UI includes five small transparent star-group sprites extracted from the
original UI reference supplied by the user for this task. Their native shapes,
colors and five-star arc are preserved. Provenance and content hashes are in
`src/setforge/defaults/ui/rating-stars.json`. These reference UI assets are
**excluded from the MIT license**; no third-party artwork rights are granted.
Replace them with artwork you are entitled to distribute for other products.

Lettering supports a user-supplied, locally licensed OTF/TTF, including Lithos Pro
Black when available. Only the local path, actual family/style and content hash
are recorded in private project/run configuration. The code never copies the
font into a project, producer ZIP, wheel or source repository. Users need an
appropriate license for their own desktop, server or application use. No font
license is granted by this repository, and no raster alphabet is redistributed.
Unconfigured/historical orders retain Pillow's bundled Aileron through Pillow's
API; a configured missing font never silently falls back. Dependencies retain
their own licenses, supplied by their packages:
Pillow (HPND and bundled-component notices), jsonschema (MIT), setuptools (MIT),
and optionally the OpenAI Python SDK (Apache-2.0). Consult the installed package
notices when redistributing a bundled application or container.

The optional image API adapter is implemented here; no copied image-generation
skill scripts or model weights are vendored. Provider names identify compatible
tools, not an endorsement or a promise of free access. No credentials are included.

Synthetic images created by tests are explicitly labelled infrastructure fixtures.
They are generated at test time and do not represent production quality evidence.
