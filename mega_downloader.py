from .crypto_fcs import *
from typing import Tuple
import re
import requests
import json
from pathlib import Path
from Crypto.Cipher import AES
from Crypto.Util import Counter
import tempfile
import shutil
import concurrent.futures
import time

def get_nodes_in_shared_folder(root_folder: str) -> dict:
    data = [{"a": "f", "c": 1, "ca": 1, "r": 1}]
    response = requests.post(
        "https://g.api.mega.co.nz/cs",
        params={'id': 0,  # self.sequence_num
                'n': root_folder},
        data=json.dumps(data)
    )
    try:
        json_resp = response.json()
        return json_resp[0]["f"]
    except TypeError as e:
        print("Error:", e)
        return None

def download_file_json(root_folder, file_id) -> dict:
    data = [{ 'a': 'g', 'g': 1, 'n': file_id }]
    response = requests.post(
        "https://g.api.mega.co.nz/cs",
        params={'id': 0,  # self.sequence_num
                'n': root_folder},
        data=json.dumps(data)
    )
    return response.json()

def parse_folder_url(url: str) -> Tuple[str, str]:
    "Returns (public_handle, key) if valid. If not returns None."
    REGEXP1 = re.compile(r"mega.[^/]+/folder/([0-z-_]+)#([0-z-_]+)(?:/folder/([0-z-_]+))*")
    REGEXP2 = re.compile(r"mega.[^/]+/#F!([0-z-_]+)[!#]([0-z-_]+)(?:/folder/([0-z-_]+))*")
    m = re.search(REGEXP1, url)
    if not m:
        m = re.search(REGEXP2, url)
    if not m:
        print("Not a valid URL")
        return None
    root_folder = m.group(1)
    key = m.group(2)
    # You may want to use m.groups()[-1]
    # to get the id of the subfolder
    return (root_folder, key)

def decrypt_node_key(key_str: str, shared_key: str) -> Tuple[int, ...]:
    encrypted_key = base64_to_a32(key_str.split(":")[1])
    return decrypt_key(encrypted_key, shared_key)


def _download_file(root_folder, file_id, file_key, dest_path=None, dest_filename=None):
    file_data = download_file_json(root_folder, file_id)[0]
    k = (file_key[0] ^ file_key[4], file_key[1] ^ file_key[5],
            file_key[2] ^ file_key[6], file_key[3] ^ file_key[7])
    iv = file_key[4:6] + (0, 0)
    meta_mac = file_key[6:8]

    file_url = file_data['g']
    file_size = file_data['s']
    attribs = base64_url_decode(file_data['at'])
    attribs = decrypt_attr(attribs, k)

    if dest_filename is not None:
        file_name = dest_filename
    else:
        file_name = attribs['n']

    input_file = requests.get(file_url, stream=True).raw

    if dest_path is None:
        dest_path = ''
    else:
        dest_path += '/'

    with tempfile.NamedTemporaryFile(mode='w+b',
                                        prefix='ankicollab_',
                                        delete=False) as temp_output_file:
        k_str = a32_to_str(k)
        counter = Counter.new(128,
                                initial_value=((iv[0] << 32) + iv[1]) << 64)
        aes = AES.new(k_str, AES.MODE_CTR, counter=counter)

        mac_str = '\0' * 16
        mac_encryptor = AES.new(k_str, AES.MODE_CBC,
                                mac_str.encode("utf8"))
        iv_str = a32_to_str([iv[0], iv[1], iv[0], iv[1]])

        for chunk_start, chunk_size in get_chunks(file_size):
            chunk = input_file.read(chunk_size)
            chunk = aes.decrypt(chunk)
            temp_output_file.write(chunk)

            encryptor = AES.new(k_str, AES.MODE_CBC, iv_str)
            for i in range(0, len(chunk) - 16, 16):
                block = chunk[i:i + 16]
                encryptor.encrypt(block)

            # fix for files under 16 bytes failing
            if file_size > 16:
                i += 16
            else:
                i = 0

            block = chunk[i:i + 16]
            if len(block) % 16:
                block += b'\0' * (16 - (len(block) % 16))
            mac_str = mac_encryptor.encrypt(encryptor.encrypt(block))
        file_mac = str_to_a32(mac_str)
        # check mac integrity
        if (file_mac[0] ^ file_mac[1],
                file_mac[2] ^ file_mac[3]) != meta_mac:
            raise ValueError('Mismatched mac')
        output_path = Path(dest_path) / file_name
        temp_output_file.close()
        shutil.move(temp_output_file.name, output_path)
        return 


def download_media_from_url(url, files, path):
    (root_folder, shared_enc_key) = parse_folder_url(url)
    shared_key = base64_to_a32(shared_enc_key)
    
    if not root_folder or not shared_key:
        return
        
    with concurrent.futures.ThreadPoolExecutor() as executor:
        # Retrieve nodes and files in parallel
        nodes_future = executor.submit(get_nodes_in_shared_folder, root_folder)
        files_dict = {file_name: False for file_name in files}

    nodes = nodes_future.result()

    if not nodes or not files_dict:
        return
    
    for node in nodes:
        is_folder = node["t"] == 1

        if is_folder:
            continue

        key = decrypt_node_key(node["k"], shared_key)
        k = (key[0] ^ key[4], key[1] ^ key[5], key[2] ^ key[6], key[3] ^ key[7])
        attrs = decrypt_attr(base64_url_decode(node["a"]), k)
        file_name = attrs["n"]
        file_id = node["h"]

        if file_name not in files_dict:
            continue

        files_dict[file_name] = True
        _download_file(root_folder, file_id, key, path)

    not_found_files = [file_name for file_name, found in files_dict.items() if not found]

    if not_found_files:
        print(f"Failed to download files: {not_found_files}")
