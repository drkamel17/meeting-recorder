"""
Restore microphone volume and unmute after auto-zoom recording.
Uses pycaw to access Windows Core Audio API.
"""
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
from ctypes import cast, POINTER
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def restore_microphone():
    """Unmute the default microphone and set volume to 90%."""
    try:
        devices = AudioUtilities.GetMicrophone()
        if devices is None:
            print("ERREUR: Aucun microphone par defaut trouve.")
            return False

        interface = devices.Activate(
            IAudioEndpointVolume._iid_, 1, None
        )
        volume = cast(interface, POINTER(IAudioEndpointVolume))

        # Check current state
        mute_val = volume.GetMute()
        current_level = volume.GetMasterVolumeLevelScalar()
        print(f"Etat actuel du microphone:")
        print(f"  Mute: {mute_val} (1=mute, 0=non mute)")
        print(f"  Volume: {current_level:.2f} (0.0 a 1.0)")

        # Unmute
        if mute_val:
            volume.SetMute(0, None)
            print("  -> Micro DEMUTE")

        # Set volume to 90%
        new_level = 0.9
        volume.SetMasterVolumeLevelScalar(new_level, None)
        print(f"  -> Volume regle a {new_level*100:.0f}%")

        # Verify
        final_mute = volume.GetMute()
        final_level = volume.GetMasterVolumeLevelScalar()
        print(f"\nEtat final du microphone:")
        print(f"  Mute: {final_mute}")
        print(f"  Volume: {final_level:.2f}")
        print("\nMicrophone restaure avec succes!")
        return True

    except Exception as e:
        print(f"ERREUR: {e}")
        return False


if __name__ == "__main__":
    restore_microphone()
