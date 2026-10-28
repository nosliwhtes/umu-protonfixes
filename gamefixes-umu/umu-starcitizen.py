"""Game fix for Star Citizen"""

import os
from protonfixes import util

def main() -> None:
    # Satisfy check for the existence of these dlls when trying to initialize ngx
    # Copy an existing DLL to these three names
    env_path = os.path.join(util.protonprefix(), 'drive_c', 'windows', 'system32')
    dest_files = ['cryptbase.dll', 'devobj.dll', 'drvstore.dll']
    for file in dest_files:
        link_path = os.path.join(env_path, file)
        if not os.path.isfile(link_path):
            os.symlink('security.dll', link_path)

    # RSI Launcher depends on powershell
    util.protontricks('powershell')

    # Prevents Error code 3221225477
    util.protontricks('vcrun2022')

    # New launcher needs dotnet48
    util.protontricks('dotnet48')

    # dotnet48 can set win7 without recording a version verb. An older win10
    # log entry therefore cannot prove that the prefix is still Windows 10.
    util.protontricks('win10', force=True)

    # Prevent RSI Launcher install and update from hanging indefinitely
    util.winedll_override('dxwebsetup.exe', util.OverrideOrder.DISABLED)
    util.winedll_override('dotNetFx45_Full_setup.exe', util.OverrideOrder.DISABLED)
