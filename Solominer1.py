import socket
import json
import hashlib
import struct
import time
import os
import base64
import http.client
import threading
import random

# ---------- Config Handling ----------
def get_input(prompt, data_type=str):
    while True:
        try:
            value = data_type(input(prompt))
            return value
        except ValueError:
            print(f"Invalid input. Please enter a valid {data_type.__name__}.")

if os.path.isfile('config.json'):
    print("config.json found, start mining")
    with open('config.json','r') as file:
        config = json.load(file)
    connection_type = config.get("connection_type", "stratum")
    pool_address = config['pool_address']
    pool_port = config["pool_port"]
    username = config["user_name"]
    password = config["password"]
    min_diff = config["min_diff"]
    rpc_user = config.get("rpc_user", "")
    rpc_password = config.get("rpc_password", "")
    rpc_port = config.get("rpc_port", 8332)
else:
    print("config.json doesn't exist, generating now")
    connection_type = get_input("Enter connection type (stratum/rpc): ").lower()
    pool_address = get_input("Enter the pool address: ")
    pool_port = get_input("Enter the pool port: ", int)
    username = get_input("Enter the user name: ")
    password = get_input("Enter the password: ")
    min_diff = get_input("Enter the minimum difficulty: ", float)
    if connection_type == "rpc":
        rpc_user = get_input("Enter Bitcoin RPC username: ")
        rpc_password = get_input("Enter Bitcoin RPC password: ")
        rpc_port = get_input("Enter Bitcoin RPC port (default 8332): ", int) or 8332
    else:
        rpc_user = ""
        rpc_password = ""
        rpc_port = 8332
    config_data = {
        "connection_type": connection_type,
        "pool_address": pool_address,
        "pool_port": pool_port,
        "user_name": username,
        "password": password,
        "min_diff": min_diff,
        "rpc_user": rpc_user,
        "rpc_password": rpc_password,
        "rpc_port": rpc_port
    }
    with open("config.json", "w") as config_file:
        json.dump(config_data, config_file, indent=4)
    print("Configuration data has been written to config.json")

# ---------- Networking ----------
def connect_to_pool(pool_address, pool_port, timeout=30, retries=5):
    for attempt in range(retries):
        try:
            print(f"Attempting to connect to pool (Attempt {attempt + 1}/{retries})...")
            sock = socket.create_connection((pool_address, pool_port), timeout)
            print("Connected to pool!")
            return sock
        except (socket.gaierror, socket.timeout, socket.error) as e:
            print(f"Connection error: {e}")
            print("Retrying in 5 seconds...")
            time.sleep(5)
    raise Exception("Failed to connect to the pool after multiple attempts")

def send_message(sock, message):
    sock.sendall((json.dumps(message) + '\n').encode('utf-8'))

def receive_messages(sock, timeout=30):
    buffer = b''
    sock.settimeout(timeout)
    while True:
        try:
            chunk = sock.recv(1024)
            if not chunk:
                break
            buffer += chunk
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                yield json.loads(line.decode('utf-8'))
        except socket.timeout:
            continue

def subscribe(sock):
    message = {"id": 1, "method": "mining.subscribe", "params": []}
    send_message(sock, message)
    for response in receive_messages(sock):
        if response.get('id') == 1:
            return response['result']

def authorize(sock, username, password):
    message = {"id": 2, "method": "mining.authorize", "params": [username, password]}
    send_message(sock, message)
    for response in receive_messages(sock):
        if response.get('id') == 2:
            return response['result']

# ---------- Mining ----------
def calculate_difficulty(hash_result):
    hash_int = int.from_bytes(hash_result[::-1], byteorder='big')
    max_target = 0xffff * (2**208)
    return max_target / hash_int

def mine(job, target, extranonce1, extranonce2_size, nonce_start=0, nonce_step=1):
    job_id, prevhash, coinb1, coinb2, merkle_branch, version, nbits, ntime, clean_jobs = job
    extranonce2 = struct.pack('<Q', 0)[:extranonce2_size]
    coinbase = (coinb1 + extranonce1 + extranonce2.hex() + coinb2).encode('utf-8')
    coinbase_hash_bin = hashlib.sha256(hashlib.sha256(coinbase).digest()).digest()
    merkle_root = coinbase_hash_bin
    for branch in merkle_branch:
        merkle_root = hashlib.sha256(
            hashlib.sha256(merkle_root + bytes.fromhex(branch)).digest()
        ).digest()
    block_header = (version + prevhash + merkle_root[::-1].hex() + ntime + nbits).encode('utf-8')
    target_bin = bytes.fromhex(target)[::-1]
    for nonce in range(nonce_start, 2**32, nonce_step):
        nonce_bin = struct.pack('<I', nonce)
        hash_result = hashlib.sha256(
            hashlib.sha256(block_header + nonce_bin).digest()
        ).digest()
        if hash_result[::-1] < target_bin:
            difficulty = calculate_difficulty(hash_result)
            if difficulty > min_diff:
                print(f"[Thread {nonce_start}] Nonce found: {nonce}, Difficulty: {difficulty}")
                return job_id, extranonce2, ntime, nonce

def submit_solution(sock, job_id, extranonce2, ntime, nonce):
    message = {
        "id": 4,
        "method": "mining.submit",
        "params": [username, job_id, extranonce2.hex(), ntime, struct.pack('<I', nonce).hex()]
    }
    send_message(sock, message)

# ---------- Thread Workers ----------
job_lock = threading.Lock()
current_job = None
stop_event = threading.Event()
num_threads = 4  # adjust for CPU cores

def listener(sock):
    global current_job
    for msg in receive_messages(sock):
        if msg.get("method") == "mining.notify":
            with job_lock:
                current_job = msg['params']

def mining_worker(thread_id, extranonce1, extranonce2_size, min_diff, sock):
    global current_job
    while not stop_event.is_set():
        with job_lock:
            job = current_job
        if job:
            result = mine(job, job[6], extranonce1, extranonce2_size,
                          nonce_start=thread_id, nonce_step=num_threads)
            if result:
                submit_solution(sock, *result)

# ---------- Main ----------
if __name__ == "__main__":
    if connection_type == "stratum":
        if pool_address.startswith("stratum+tcp://"):
            pool_address = pool_address[len("stratum+tcp://"):]
        sock = connect_to_pool(pool_address, pool_port)
        extranonce = subscribe(sock)
        extranonce1, extranonce2_size = extranonce[1], extranonce[2]
        authorize(sock, username, password)
        # Start listener
        threading.Thread(target=listener, args=(sock,), daemon=True).start()
        # Start miners
        for i in range(num_threads):
            threading.Thread(target=mining_worker,
                             args=(i, extranonce1, extranonce2_size, min_diff, sock),
                             daemon=True).start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Stopping miner...")
            stop_event.set()
    else:
        print("Only Stratum threaded version implemented.")