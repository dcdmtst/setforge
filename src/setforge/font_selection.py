"""Local font discovery, explicit producer choice and OS-specific install help."""
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import webbrowser

from PIL import ImageFont
from .io import read
from .typography import NEUTRAL, Lettering, font_profile, project_profile

DEFAULT_FONT='Lithos Pro Black'
ADOBE_URL='https://fonts.adobe.com/fonts/lithos'
_cache=(0,[])


def clean_name(name):
    if not isinstance(name,str) or not name.strip() or len(name)>160 or any(ord(c)<32 for c in name):
        raise ValueError('Укажите название шрифта (до 160 символов).')
    return name.strip()


def key(name):return ''.join(c for c in name.casefold() if c.isalnum())


def requested_font(project,name=None):
    if name is not None:return clean_name(name)
    cfg=read(Path(project)/'project.json')
    if 'font' in cfg:return clean_name(cfg['font'])
    profile=project_profile(project)
    if profile['kind']=='local-opentype':return profile['family']+' '+profile['style']
    return DEFAULT_FONT


def font_paths():
    home=Path.home();system=platform.system()
    if system=='Windows':
        roots=[Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts',
               Path(os.environ.get('LOCALAPPDATA',home/'AppData/Local'))/'Microsoft/Windows/Fonts',
               Path(os.environ.get('APPDATA',home/'AppData/Roaming'))/'Adobe/CoreSync/plugins/livetype/.r']
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER,winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive,r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts') as registry:
                    for i in range(winreg.QueryInfoKey(registry)[1]):
                        _,value,_=winreg.EnumValue(registry,i)
                        path=Path(value)
                        if not path.is_absolute():path=roots[0]/path
                        yield path
            except OSError:pass
    elif system=='Darwin':
        roots=[home/'Library/Fonts',Path('/Library/Fonts'),Path('/System/Library/Fonts'),
               home/'Library/Application Support/Adobe/CoreSync/plugins/livetype/.r']
    else:roots=[home/'.local/share/fonts',home/'.fonts',Path('/usr/share/fonts'),Path('/usr/local/share/fonts')]
    for root in roots:
        if root.is_dir():
            for path in root.rglob('*'):
                if path.is_file() and (path.suffix.lower() in ('.ttf','.otf') or root.name=='.r'):
                    yield path


def installed_fonts(force=False):
    global _cache
    if not force and time.monotonic()-_cache[0]<10:return _cache[1]
    found=[];seen=set()
    for path in font_paths():
        if path in seen:continue
        seen.add(path)
        try:
            if path.stat().st_size>20_000_000:continue
            family,style=ImageFont.truetype(str(path),64).getname()
            found.append({'name':family+' '+style,'family':family,'style':style,'path':str(path)})
        except (OSError,ValueError):continue
    _cache=(time.monotonic(),found)
    return found


def _profile(path,name):
    profile=font_profile(path);profile['requested']=name
    return profile


def find_profile(project,name):
    name=clean_name(name)
    if key(name) in ('default','aileron','pillowaileron','neutral'):return dict(NEUTRAL)
    if key(name)=='lithos':name=DEFAULT_FONT
    saved=project_profile(project)
    if saved['kind']=='local-opentype' and key(name) in (key(saved['family']),key(saved['family']+' '+saved['style'])):
        lettering=Lettering(saved)
        if lettering.font is not None:return {**saved,'requested':name}
    fonts=installed_fonts()
    exact=[f for f in fonts if key(f['name'])==key(name)]
    choices=exact or [f for f in fonts if key(f['family'])==key(name)]
    choices.sort(key=lambda f:(f['style'].lower() not in ('regular','normal','roman'),f['name'],f['path']))
    for item in choices:
        try:return _profile(item['path'],name)
        except (OSError,ValueError):continue
    return None


def installation_help(name,system=None):
    system=system or platform.system();lithos=key(name) in ('lithos','lithosproblack')
    steps=[]
    if lithos:
        if system in ('Windows','Darwin'):
            steps=['Установите официальный Creative Cloud desktop и войдите в Adobe.',
                   'Откройте Lithos в Adobe Fonts и добавьте начертание Black.',
                   'В Creative Cloud откройте Fonts → Added fonts → Lithos Pro Black → Install (для других приложений).']
        else:steps=['Creative Cloud desktop не поддерживает Linux. Нужен отдельно лицензированный OTF/TTF Lithos Pro Black или дефолтный Aileron.']
    if system=='Windows':steps+=['Если у вас есть лицензированный OTF/TTF: правой кнопкой по файлу → Установить.']
    elif system=='Darwin':steps+=['Если у вас есть лицензированный OTF/TTF: откройте его в Font Book (Шрифты) → Установить шрифт.']
    else:steps+=['Поместите лицензированный OTF/TTF в ~/.local/share/fonts и обновите кэш командой fc-cache -f.']
    steps+=['Вернитесь в Setforge и нажмите «Проверить установку». Если шрифт не найден, укажите локальный файл OTF/TTF или используйте дефолтный.']
    return {'os':system,'font':name,'steps':steps,
            'url':ADOBE_URL if lithos else None,
            'download_url':'https://www.adobe.com/download/creative-cloud' if lithos and system in ('Windows','Darwin') else None,
            'automatic_note':'Автоматически устанавливается только ваш локальный лицензированный файл. Для Adobe откроется официальный способ установки; вход, условия и оплату программа за вас не подтверждает.'}


class FontChoiceRequired(ValueError):
    def __init__(self,name):
        super().__init__(f'Шрифт {name} не установлен. Установить шрифт или использовать дефолтный Aileron?')
        self.details={'error':str(self),'code':'font_choice_required','font':name,'instructions':installation_help(name)}


def selection(project,name=None,decision=None):
    if decision not in (None,'default'):raise ValueError('Unknown font decision.')
    name=requested_font(project,name)
    if decision=='default':
        return {**NEUTRAL,'requested':name,'fallback_reason':'Default Aileron selected for this order.'}
    profile=find_profile(project,name)
    if profile is not None:return profile
    raise FontChoiceRequired(name)


def available_or_default(project,name=None):
    try:return selection(project,name)
    except FontChoiceRequired:return selection(project,name,'default')


def context(project,name=None):
    name=requested_font(project,name);profile=find_profile(project,name)
    return {'requested':name,'available':profile is not None,
            'effective':Lettering(profile).describe() if profile is not None else 'Pillow Aileron (default)',
            'options':sorted(set([DEFAULT_FONT,'default']+[f['name'] for f in installed_fonts()])),
            'instructions':installation_help(name)}


def install_local_file(source,name):
    """Called only after the user explicitly chooses automatic installation."""
    source=Path(source).expanduser().resolve();profile=font_profile(source)
    if key(name) not in (key(profile['family']),key(profile['family']+' '+profile['style'])):
        raise ValueError('Файл не соответствует выбранному семейству и начертанию.')
    system=platform.system();home=Path.home()
    if system=='Windows':root=Path(os.environ.get('LOCALAPPDATA',home/'AppData/Local'))/'Microsoft/Windows/Fonts'
    elif system=='Darwin':root=home/'Library/Fonts'
    else:root=home/'.local/share/fonts'
    root.mkdir(parents=True,exist_ok=True)
    suffix='.otf' if source.read_bytes()[:4]==b'OTTO' else '.ttf'
    target=root/('setforge-'+profile['sha256'][:16]+suffix)
    if target.exists() and target.read_bytes()!=source.read_bytes():raise ValueError('Existing font file differs; refusing to overwrite.')
    if not target.exists():shutil.copy2(source,target)
    if system=='Windows':
        import ctypes,winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts') as registry:
            winreg.SetValueEx(registry,profile['family']+' '+profile['style']+' (Setforge)',0,winreg.REG_SZ,str(target))
        if not ctypes.windll.gdi32.AddFontResourceExW(str(target),0,None):
            raise ValueError('Windows не активировал файл шрифта. Попробуйте ручную установку.')
        ctypes.windll.user32.SendMessageTimeoutW(0xffff,0x001d,0,0,2,1000,None)
    elif system!='Darwin' and shutil.which('fc-cache'):
        subprocess.run(['fc-cache','-f',str(root)],check=True,timeout=30,capture_output=True)
    installed_fonts(force=True)
    return {'status':'installed','font':profile['family']+' '+profile['style']}


def try_install(project,name,source=None):
    name=clean_name(name)
    if source:
        try:return install_local_file(source,name)
        except (OSError,ValueError,subprocess.SubprocessError) as error:
            return {'status':'manual_step_required','instructions':installation_help(name),
                    'message':'Автоустановка не завершена: '+str(error)+'. Установите вручную или используйте дефолтный Aileron.'}
    installed_fonts(force=True)
    if find_profile(project,name):return {'status':'installed','font':name}
    help=installation_help(name)
    if help['url']:
        webbrowser.open(help['url'])
        if platform.system()=='Windows':
            exe=Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Adobe/Adobe Creative Cloud/ACC/Creative Cloud.exe'
            if exe.is_file():subprocess.Popen([str(exe)],creationflags=0x08000000)
        elif platform.system()=='Darwin':
            try:subprocess.run(['open','-a','Adobe Creative Cloud'],timeout=10,capture_output=True)
            except (OSError,subprocess.SubprocessError):pass
    return {'status':'manual_step_required','instructions':help,
            'message':'Автоматическая установка не завершена. Укажите лицензированный файл или завершите установку через Adobe и нажмите «Проверить установку».'}
