import asyncio
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime

# --- IMPORT TELETHON ---
try:
    from telethon import TelegramClient
    from telethon.errors import (
        FloodWaitError,
        InviteHashExpiredError,
        InviteHashInvalidError,
        InviteRequestSentError,
        UserAlreadyParticipantError,
        UsernameInvalidError,
        UsernameNotOccupiedError,
        YouBlockedUserError,
    )
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest, StartBotRequest
    from telethon.sessions import StringSession
except ImportError:
    print("ERROR: Telethon tidak terinstall.")
    sys.exit(1)

logging.getLogger("telethon").setLevel(logging.CRITICAL)

# ==========================================
# KONFIGURASI DARI ENVIRONMENT VARIABLES
# ==========================================
API_ID = int(os.getenv('TELETHON_API_ID', '0'))
API_HASH = os.getenv('TELETHON_API_HASH', '')
STRING_SESSION = os.getenv('TELETHON_STRING_SESSION', '')

if not API_ID or not API_HASH or not STRING_SESSION:
    print("FATAL ERROR: Missing Secrets! Cek Settings -> Secrets and variables -> Actions.")
    sys.exit(1)

# Nama File Input & Output
INPUT_CSV = "start_links.csv"   # Langsung baca file bersih Anda
OUTPUT_CSV = "hasil.csv"
JOIN_STATE_FILE = "join_state.json"
LINK_STATE_FILE = "links_diakses.json" 

# Parameter Operasi
MAKS_PER_JALAN = 0       # 0 = TANPA BATAS
ISTIRAHAT_BATCH = 2      # Istirahat singkat antar batch besar
JEDA_ANTAR_BOT = 0.5     # Jeda sangat pendek antar bot (akun kuat)
TIMEOUT_BOT = 60         # Batas total waktu untuk satu bot (termasuk wait)
JEDA_JOIN = 2            # Jeda setelah join group (bisa dipercepat jika aman)
JOIN_ENABLED = True      
MAKS_JOIN_PER_HARI = 50  # Naikkan sedikit batas harian

HEADER = ["bot", "start_code", "status", "links", "hasil_join"]
LINK_RE = re.compile(r"https?://t\.me/[^\s)>\]\"']+")
KATA_MATI = ("link_mati", "expired", "not valid", "bot_tidak_ada", "bot_diblokir")


class Berhenti(Exception):
    pass


def get_today_str():
    return datetime.utcnow().strftime("%Y-%m-%d")


def link_mati(teks):
    t = (teks or "").lower()
    return any(k in t for k in KATA_MATI)


# ==========================================
# CATATAN LINK YANG SUDAH DIAKSES
# ==========================================
def norm_link(link):
    l = (link or "").strip().rstrip("/").lower()
    l = re.sub(r"^https?://(www\.)?", "", l)
    return l


def muat_link_diakses():
    try:
        if os.path.exists(LINK_STATE_FILE):
            with open(LINK_STATE_FILE, encoding="utf-8") as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()


def simpan_link_diakses():
    with open(LINK_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(LINK_DIAKSES), f)


LINK_DIAKSES = muat_link_diakses()


def tandai_diakses(link):
    LINK_DIAKSES.add(norm_link(link))
    simpan_link_diakses()


# ==========================================
# STATE JOIN HARIAN
# ==========================================
def jumlah_join_hari_ini():
    try:
        if os.path.exists(JOIN_STATE_FILE):
            with open(JOIN_STATE_FILE, encoding='utf-8') as f:
                d = json.load(f)
            if d.get("tanggal") == get_today_str():
                return int(d.get("jumlah", 0))
    except Exception:
        pass
    return 0


def tambah_join():
    n = jumlah_join_hari_ini() + 1
    data = {"tanggal": get_today_str(), "jumlah": n}
    with open(JOIN_STATE_FILE, "w", encoding='utf-8') as f:
        json.dump(data, f)


def ambil_link(msg):
    links = set(LINK_RE.findall(msg.text or ""))
    for e in msg.entities or []:
        url = getattr(e, "url", None)
        if url and "t.me" in url:
            links.add(url)
    for baris in msg.buttons or []:
        for tombol in baris:
            if hasattr(tombol, 'url') and tombol.url and "t.me" in tombol.url:
                links.add(tombol.url)
    return sorted(links)


async def gabung(client, link):
    if "start=" in link:
        return "dilewati_start_param"

    m1 = re.search(r"t\.me/(?:\+|joinchat/)([\w-]+)", link)
    m2 = re.search(r"t\.me/([A-Za-z0-9_]{4,})", link)

    if m1:
        aksi = ImportChatInviteRequest(m1.group(1))
    elif m2 and not m2.group(1).lower().endswith("bot"):
        aksi = JoinChannelRequest(m2.group(1))
    else:
        return "format_invalid"

    # Sudah pernah diakses -> jangan diakses lagi
    if norm_link(link) in LINK_DIAKSES:
        return "sudah_diakses"

    if jumlah_join_hari_ini() >= MAKS_JOIN_PER_HARI:
        raise Berhenti(f"Batas gabung harian ({MAKS_JOIN_PER_HARI}) tercapai.")

    try:
        await client(aksi)
        tambah_join()
        tandai_diakses(link)
        return "gabung_sukses"
    except (InviteHashExpiredError, InviteHashInvalidError):
        tandai_diakses(link)
        return "link_mati"
    except UserAlreadyParticipantError:
        tandai_diakses(link)
        return "sudah_anggota"
    except InviteRequestSentError:
        tandai_diakses(link)
        return "menunggu_persetujuan_admin"
    except FloodWaitError as e:
        # Tidak ditandai: link belum benar-benar diproses
        raise Berhenti(f"Flood Wait saat join: {e.seconds}s. Stop sekarang.")
    except Exception as e:
        tandai_diakses(link)
        return f"gagal_join: {str(e)[:50]}"


async def mulai(client, entity, code):
    terakhir = await client.get_messages(entity, limit=1)
    sent_id = terakhir[0].id if terakhir else 0
    await client(StartBotRequest(bot=entity, peer=entity, start_param=code))
    return sent_id


async def smart_wait_reply(client, entity, sent_id, timeout=15):
    """
    Menunggu balasan secara dinamis.
    Cek setiap 0.5 detik apakah ada pesan BARU (ID > sent_id).
    Return: List of messages found after sent_id.
    """
    start_time = time.time()
    new_msgs = []
    
    while time.time() - start_time < timeout:
        # Ambil pesan terbaru dari chat
        msgs = await client.get_messages(entity, limit=5)
        
        # Filter hanya pesan yang ID-nya lebih besar dari pesan kita kirim
        filtered = [m for m in msgs if m.id > sent_id]
        
        if filtered:
            # Urutkan berdasarkan ID ascending (terlama dulu)
            filtered.sort(key=lambda x: x.id)
            return filtered
            
        await asyncio.sleep(0.5) # Cek ulang setengah detik kemudian
        
    return [] # Timeout, tidak ada balasan


async def proses(client, bot, code):
    entity = await client.get_entity(bot)
    sent_id = await mulai(client, entity, code)
    
    # SMART WAIT: Tunggu sampai bot membalas (maks 15 detik)
    # Ini menggantikan sleep fixed yang boros waktu
    messages = await smart_wait_reply(client, entity, sent_id, timeout=15)
    
    if not messages:
        return "timeout_no_response", [], []

    links = []
    media_count = 0
    
    # Ekstrak semua link dan hitung media dari seluruh balasan
    for m in messages:
        if m.media and not m.web_preview:
            media_count += 1
            
        for l in ambil_link(m):
            if l not in links:
                links.append(l)

    status = "ada_link" if links else ("balasan_tanpa_link" if messages else "tanpa_balasan")
    if media_count > 0:
        status += f"_media={media_count}"
    
    hasil_join = []

    if JOIN_ENABLED and links:
        berhasil = 0
        for l in links:
            r = await gabung(client, l)
            hasil_join.append(r)
            if r == "link_mati":
                break 
            if r == "gabung_sukses":
                berhasil += 1
                await asyncio.sleep(JEDA_JOIN)
        
        if berhasil:
            # Refresh state setelah join
            sent_id_new = await mulai(client, entity, code)
            msgs_new = await smart_wait_reply(client, entity, sent_id_new, timeout=10)
            _, _, media_new = await baca_balasan_simple(msgs_new)
            status = f"gabung_lalu_update_media={media_new}"

    return status, links, hasil_join


async def baca_balasan_simple(messages):
    links = []
    for m in messages:
        for l in ambil_link(m):
            if l not in links:
                links.append(l)
    media = sum(1 for m in messages if m.media and not m.web_preview)
    return links, len(messages), media


def siapkan_hasil():
    if os.path.exists(OUTPUT_CSV):
        with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if not set(HEADER) <= set(header):
            backup_name = f"hasil_backup_{int(time.time())}.csv"
            os.rename(OUTPUT_CSV, backup_name)
            print(f"Header mismatch. Old result backed up to {backup_name}")
            
    if not os.path.exists(OUTPUT_CSV):
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(HEADER)


def muat_hasil():
    selesai, mati = set(), set()
    if not os.path.exists(OUTPUT_CSV):
        return selesai, mati
        
    with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            key = r["bot"].strip().lstrip("@") + "|" + r["start_code"].strip()
            selesai.add(key)
            if link_mati(r.get("hasil_join")):
                mati.add(r["bot"].strip().lstrip("@"))
    return selesai, mati


async def kerjakan_satu(client, bot, code):
    retries = 2
    for attempt in range(retries):
        try:
            if not client.is_connected():
                await client.connect()
            return await asyncio.wait_for(proses(client, bot, code), TIMEOUT_BOT)
        except Berhenti:
            raise
        except FloodWaitError as e:
            wait_sec = min(int(e.seconds), 60) 
            print(f"Flood Wait detected. Sleeping {wait_sec}s...")
            await asyncio.sleep(wait_sec)
            if attempt == retries - 1:
                 return f"flood_limit_exceeded", [], []
        except YouBlockedUserError:
            return "bot_diblokir_user", [], ["blocked_by_user"]
        except (UsernameNotOccupiedError, UsernameInvalidError, ValueError):
            return "bot_not_found", [], ["invalid_username"]
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            print(f"Network Error (Attempt {attempt+1}/{retries}): {e}")
            await asyncio.sleep(2)
        except Exception as e:
            return f"unexpected_error: {str(e)[:100]}", [], []
    return None


async def satu_putaran(client, antrean, bot_mati):
    diproses = 0
    
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        
        for row in antrean:
            if MAKS_PER_JALAN > 0 and diproses >= MAKS_PER_JALAN:
                break
                
            bot_raw = row["bot"].strip().lstrip("@")
            code = row["start_code"].strip()
            
            if bot_raw in bot_mati:
                w.writerow([bot_raw, code, "skipped_dead_bot", "", "dead"])
                out.flush()
                print(f"[SKIP] {bot_raw}: Marked Dead")
                continue

            hasil = await kerjakan_satu(client, bot_raw, code)
            
            if hasil is None:
                print("Critical Network Failure. Stopping this batch.")
                break
                
            status, links, hj = hasil
            
            if link_mati(" | ".join(hj)):
                bot_mati.add(bot_raw)

            diproses += 1
            w.writerow([bot_raw, code, status, " ".join(links), " | ".join(hj)])
            out.flush() 
            print(f"[{diproses}] {bot_raw}: {status}")
            
            # Jeda super pendek antar bot
            await asyncio.sleep(JEDA_ANTAR_BOT)


async def main():
    print(f"Starting Bot Process... Date: {get_today_str()}")
    print(f"Mode: {'UNLIMITED' if MAKS_PER_JALAN == 0 else f'Max {MAKS_PER_JALAN}'}")
    print(f"Link sudah diakses sebelumnya: {len(LINK_DIAKSES)}")

    # Initialize Client using String Session
    client = TelegramClient(
        StringSession(STRING_SESSION),
        API_ID,
        API_HASH,
        connection_retries=3,
        retry_delay=5,
        auto_reconnect=True,
    )

    await client.connect()

    if not await client.is_user_authorized():
        print("CRITICAL ERROR: Invalid String Session or Account Blocked.")
        await client.disconnect()
        sys.exit(1)

    me = await client.get_me()
    print(f"Logged in as: {me.first_name} (@{me.username})")

    # Load Input Data Langsung dari CSV (Karena file sudah bersih)
    if not os.path.exists(INPUT_CSV):
        print(f"Input file {INPUT_CSV} not found!")
        await client.disconnect()
        sys.exit(1)

    try:
        with open(INPUT_CSV, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        print(f"Gagal membaca CSV: {e}")
        await client.disconnect()
        sys.exit(1)
        
    print(f"Loaded {len(rows)} entries from {INPUT_CSV}")

    if len(rows) == 0:
        print("File kosong. Tidak ada yang diproses.")
        await client.disconnect()
        return

    siapkan_hasil()

    try:
        while True:
            selesai_set, bot_mati_set = muat_hasil()
            
            antrean = []
            for r in rows:
                b = r["bot"].strip().lstrip("@")
                c = r["start_code"].strip()
                key = f"{b}|{c}"
                if key not in selesai_set:
                    antrean.append(r)
            
            total_processed = len(selesai_set)
            remaining = len(antrean)
            
            print(f"Status: Total Done: {total_processed} | Remaining Queue: {remaining} | Dead Bots Known: {len(bot_mati_set)}")
            
            if not antrean:
                print("All tasks completed successfully.")
                break
                
            await satu_putaran(client, antrean, bot_mati_set)
            
            if jumlah_join_hari_ini() >= MAKS_JOIN_PER_HARI:
                print(f"Daily Join Limit ({MAKS_JOIN_PER_HARI}) reached. Pausing further joins.")
                break

            print(f"Istirahat singkat {ISTIRAHAT_BATCH}s sebelum cek ulang antrian...")
            await asyncio.sleep(ISTIRAHAT_BATCH)
            
    except Berhenti as e:
        print(f"\nSTOPPED BY LOGIC: {e}")
    except KeyboardInterrupt:
        print("\nInterrupted by user/system.")
    finally:
        await client.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
