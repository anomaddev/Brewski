"""Include the built dashboard in the wheel when web/dist exists.

Dev installs do not need the build. `npm run build` in web/ before
`pip install .` is what puts the UI on a Raspberry Pi.
"""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        dist = Path(self.root) / "web" / "dist"
        if (dist / "index.html").is_file():
            build_data["force_include"][str(dist)] = "brewski/dist"
