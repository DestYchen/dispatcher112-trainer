"""Build the eight WAV assets once; never imported or called by the application."""
import argparse
import hashlib
import json
import urllib.request
import wave
from pathlib import Path

import torch

MODEL_URL = 'https://models.silero.ai/models/tts/ru/v5_cis_base_nostress.pt'
VOICES = {
    'male_calm': ('ru_dmitriy', 'Слушаю вас.', 'Я вас понял, информация принята.'),
    'male_brisk': ('ru_igor', 'Главный инженер. Докладывайте.', 'Принято. Приступаю к организации работ.'),
    'female_calm': ('ru_ekaterina', 'Служба сто двенадцать. Слушаю вас.', 'Информация принята и зарегистрирована.'),
    'female_brisk': ('ru_zinaida', 'Начальник смены. Докладывайте обстановку.', 'Я вас поняла. Действуйте по обстановке.'),
}
ACCENTS = {
    'Слушаю вас.': 'Сл+ушаю вас.',
    'Я вас понял, информация принята.': 'Я вас п+онял, информ+ация принят+а.',
    'Главный инженер. Докладывайте.': 'Гл+авный инжен+ер. Докл+адывайте.',
    'Принято. Приступаю к организации работ.': 'Пр+инято. Приступ+аю к организ+ации раб+от.',
    'Служба сто двенадцать. Слушаю вас.': 'Сл+ужба сто двен+адцать. Сл+ушаю вас.',
    'Информация принята и зарегистрирована.': 'Информ+ация принят+а и зарегистр+ирована.',
    'Начальник смены. Докладывайте обстановку.': 'Нач+альник см+ены. Докл+адывайте обстан+овку.',
    'Я вас поняла. Действуйте по обстановке.': 'Я вас понял+а. Д+ействуйте по обстан+овке.',
}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=Path, default=Path('.tools/silero-v5-cis-base-nostress.pt'))
    parser.add_argument('--output', type=Path, default=Path('data/voices'))
    parser.add_argument('--source-url', default=MODEL_URL)
    args = parser.parse_args()
    args.model.parent.mkdir(parents=True, exist_ok=True)
    if not args.model.is_file():
        print('Downloading the build-time Silero model from its publisher', flush=True)
        urllib.request.urlretrieve(args.source_url, args.model)
    torch.set_num_threads(4)
    torch.manual_seed(0)
    model = torch.package.PackageImporter(str(args.model)).load_pickle('tts_models', 'model')
    model.to(torch.device('cpu'))
    manifest = {'model_url': MODEL_URL, 'download_source': args.source_url, 'model_sha256':hashlib.sha256(args.model.read_bytes()).hexdigest(),
                'license':'MIT', 'license_url':'https://github.com/snakers4/silero-models/blob/master/LICENSE_CIS',
                'torch_version':torch.__version__, 'sample_rate':24000, 'voices':{}}
    for voice, (speaker, greeting, confirmation) in VOICES.items():
        target = args.output / voice
        target.mkdir(parents=True, exist_ok=True)
        manifest['voices'][voice] = {'speaker':speaker, 'greeting':greeting, 'confirmation':confirmation, 'files':{}}
        for phrase, text in (('greeting',greeting),('confirm',confirmation)):
            with torch.inference_mode():
                audio = model.apply_tts(text=ACCENTS[text], speaker=speaker, sample_rate=24000)
            pcm = audio.clamp(-1,1).mul(32767).to(torch.int16).cpu().numpy().tobytes()
            path = target / (phrase + '.wav')
            with wave.open(str(path),'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24000)
                output.writeframes(pcm)
            manifest['voices'][voice]['files'][phrase] = hashlib.sha256(path.read_bytes()).hexdigest()
            print(f'{voice}/{phrase}.wav: {len(pcm)//2/24000:.2f} seconds', flush=True)
    args.output.joinpath('manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__ == '__main__':
    main()
