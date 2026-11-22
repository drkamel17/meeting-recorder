"""
Diagnostiquer le microphone en detail - verifier le boost et les proprietes.
"""
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
from ctypes import cast, POINTER


def diagnose_microphone():
    try:
        devices = AudioUtilities.GetMicrophone()
        if devices is None:
            print("ERREUR: Aucun microphone par defaut trouve.")
            return

        interface = devices.Activate(IAudioEndpointVolume._iid_, 1, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))

        mute = volume.GetMute()
        level = volume.GetMasterVolumeLevelScalar()
        db = volume.GetMasterVolumeLevel()
        count = volume.GetChannelCount()
        step, step_count = volume.GetVolumeStepInfo()

        print(f"=== Volume du microphone ===")
        print(f"  Mute: {mute}")
        print(f"  Volume (0-1): {level}")
        print(f"  Volume (dB): {db}")
        print(f"  Channels: {count}")
        print(f"  Volume step: {step}/{step_count}")

        # Lister tous les devices
        print(f"\n=== Peripheriques audio ===")
        try:
            import sounddevice as sd
            devices_list = sd.query_devices()
            for i, d in enumerate(devices_list):
                if d['max_input_channels'] > 0:
                    print(f"  [{i}] {d['name']} - in:{d['max_input_channels']} ch - rate:{d['default_samplerate']}")
        except Exception as e:
            print(f"  (sounddevice error: {e})")

        print(f"\n=== Diagnostic ===")
        print(f"Le volume du micro est a {level*100:.0f}% ({db:.1f} dB)")
        print()
        if db < -30:
            print("PROBLEME: Le volume en dB est tres bas (< -30 dB)")
            print("Le micro est probablement desactive ou le boost est a 0.")
        elif level > 0.5 and db < -20:
            print("PROBLEME: Volume software OK mais signal tres faible")
            print("Le 'Microphone Boost' est probablement a 0 dB.")
        else:
            print("Volume OK au niveau logiciel.")

        print()
        print("SOLUTION:")
        print("1. Panneau Son > Enregistrement > Microphone > Niveaux")
        print("2. Augmenter 'Microphone Boost' a +10 dB ou +20 dB")
        print("3. Verifier que le bon micro est selectionne")

    except Exception as e:
        print(f"ERREUR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    diagnose_microphone()
