# Test TTS correctly (no result variable error)
import asyncio
from app.services.tts import TTSService

async def test():
    s = TTSService()
    try:
        result = await s.synthesize('Тест финальной проверки.', voice='male')
        print('TTS OK:', len(result.audio_bytes), 'bytes')
    except Exception as e:
        print('TTS ERR:', e)
    finally:
        await s.close()

asyncio.run(test())