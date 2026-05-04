import base64
import logging
from google.cloud import texttospeech
from google.api_core.exceptions import GoogleAPIError

logger = logging.getLogger(__name__)

def generate_tts_base64(text: str, language_code: str = 'en-US') -> str:
    """
    Generates text-to-speech using Google Cloud TTS and returns it as a base64 string.
    If it fails, returns None.
    """
    if not text:
        return None

    try:
        # Instantiate a client
        client = texttospeech.TextToSpeechClient()

        # Set the text input to be synthesized
        synthesis_input = texttospeech.SynthesisInput(text=text)

        # Build the voice request, select the language code and the ssml voice gender
        if language_code.startswith('en'):
            name = "en-US-Journey-F" # Example of a good journey/studio voice, falling back to standard
            voice = texttospeech.VoiceSelectionParams(
                language_code="en-US",
                name="en-US-Journey-F" # Try journey voice for nice quality
            )
        else:
            voice = texttospeech.VoiceSelectionParams(
                language_code=language_code,
                ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
            )

        # Select the type of audio file you want returned
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3
        )

        # Perform the text-to-speech request
        # We catch exceptions to fallback gracefully if credentials are not configured
        response = client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config
        )

        # Return the base64 encoded audio content
        return base64.b64encode(response.audio_content).decode('utf-8')

    except GoogleAPIError as e:
        logger.error(f"Google Cloud TTS API Error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in TTS generation: {e}")
        return None
