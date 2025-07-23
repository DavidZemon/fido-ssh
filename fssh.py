#!/usr/bin/env python3
import os
import subprocess
import sys
import re
import logging

# Default values
YUBICO_VID = "1050"  # https://www.yubico.com/
TOKEN_VID = "3815"  # https://www.tokenring.com/

# Load config from ~/.config/fsshrc if it exists
fssh_config = os.path.expanduser("~/.config/fsshrc")
config: dict[str, str] = {}
if os.path.isfile(fssh_config):
    with open(fssh_config) as f:
        for line in f:
            if '=' in line:
                k, v = line.strip().split('=', 1)
                config[k.strip()] = v.strip().strip('"')


def get_vid_list() -> list[str]:
    if 'fssh_vid_list' in config:
        return list(config['fssh_vid_list'].split())
    else:
        return [YUBICO_VID, TOKEN_VID]


fssh_vid_list = get_vid_list()
use_docker = '1' == config.get('use_docker', os.environ.get('use_docker', '1'))
docker_container_name = config.get('docker_container_name', os.environ.get('docker_container_name', 'fido-usbipd'))

logging.info(f"Using FIDO USB VID list: {fssh_vid_list}")


def check_kernel_modules():
    mods = subprocess.check_output(['sudo', 'lsmod']).decode()
    for mod in ['usbip_host', 'vhci_hcd']:
        if mod in mods:
            logging.info(f"Kernel module {mod} already loaded")
        else:
            logging.info(f"Loading kernel module {mod}")
            if subprocess.call(['sudo', 'modprobe', mod]) != 0:
                logging.error(f"ERROR: Exiting {os.path.basename(sys.argv[0])} due to kernel module failure")
                sys.exit(1)


def find_fido_devices() -> list[str]:
    fido_docker_args: list[str] = []
    lsusb = subprocess.check_output(['lsusb']).decode().splitlines()
    vid_regex = '|'.join(fssh_vid_list)
    for device in lsusb:
        if re.search(rf'ID ({vid_regex}):', device):
            prefix = device.split(':')[0]
            parts = prefix.split()
            bus = parts[1]
            port = parts[3]
            path = f"/dev/bus/usb/{bus}/{port}"
            logging.info(f"Found FIDO device: {path}")
            fido_docker_args.extend(['--device', path])
    if not fido_docker_args:
        logging.error(f"No FIDO devices found. Exiting {os.path.basename(sys.argv[0])}")
        sys.exit(1)
    return fido_docker_args


def find_usbip_bus_ids() -> list[str]:
    usbip_lines = subprocess.check_output([
        'docker', 'run', '-t', '--rm', '--privileged', '-v', '/dev:/dev', '--name', 'usbip-discovery',
        'usbipd', 'usbip', 'list', '--local'
    ])
    bus_id_lines = [bus_id_line for bus_id_line in usbip_lines.decode().splitlines() if '- busid' in bus_id_line]

    bus_ids: list[str] = []
    for bus_id_line in bus_id_lines:
        for vid in fssh_vid_list:
            if f' ({vid}:' in bus_id_line:
                bus_id = bus_id_line.split()[2]
                logging.info(f"Found FIDO device with usbip bus ID: {bus_id}")
                bus_ids.append(bus_id)

    if not bus_ids:
        logging.error("ERROR: Failed to find any matching bus IDs reported by usbip")
        sys.exit(1)

    return bus_ids


def run_usbip(args: list[str]):
    if not use_docker:
        return subprocess.run(['sudo', 'usbip'] + args, check=True)
    else:
        return subprocess.run(['docker', 'exec', docker_container_name, 'usbip'] + args, check=True)


def unbind_and_stop_container(bus_ids: list[str]):
    retval = 0
    if not bus_ids:
        logging.error("ERROR: No bus IDs provided to unbind")
        return 1
    for busid in bus_ids:
        logging.info(f"Unbinding usbip bus ID {busid}")
        if not run_usbip(['unbind', '-b', busid]):
            logging.error(f"ERROR: Failed to unbind usbip bus ID {busid}")
            retval = 1
    if not use_docker:
        subprocess.call(['sudo', 'killall', 'usbipd'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.call(['docker', 'stop', '-t', '5', docker_container_name], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
    return retval


def main():
    check_kernel_modules()
    fido_docker_args = find_fido_devices()
    bus_ids = find_usbip_bus_ids()

    # Start usbip daemon
    if not use_docker:
        subprocess.run(['usbipd', '--daemon'], check=True)
    else:
        subprocess.run([
            'docker', 'run', '-d', '--rm', '--privileged',
            '-p', '127.0.0.1:3240:3240/tcp',
            *fido_docker_args,
            '--name', docker_container_name,
            'usbipd', 'usbipd', '--debug'
        ], check=True)

    # Bind FIDO devices to the usbip daemon
    bound_ids: list[str] = []
    for bus_id in bus_ids:
        try:
            run_usbip(['bind', '-b', bus_id])
            logging.info(f"Successfully bound usbip bus ID {bus_id}")
            bound_ids.append(bus_id)
        except subprocess.CalledProcessError:
            logging.error(f"ERROR: Failed to bind usbip bus ID {bus_id}")
            unbind_and_stop_container(bound_ids)
            sys.exit(1)

    # Open the SSH connection
    logging.info(f"Opening SSH connection now. Will bind to bus IDs {bound_ids}")
    attach_commands: list[str] = []
    detach_commands: list[str] = []
    for index, bus_id in enumerate(bound_ids):
        attach_commands.append(
            # Dump the logs from the first attachment, but ignore all logs from the detachment and reattachment
            f'sudo usbip attach -r localhost -b {bus_id} && ' +
            f'sudo usbip detach -p {index} 2&>1 > /dev/null && ' +
            f'sudo usbip attach -r localhost -b {bus_id} 2&>1 > /dev/null'
        )
        detach_commands.append(f'sudo usbip detach -p {index}')
    ssh_cmd = [
        'ssh', '-t', '-R', '3240:localhost:3240', *sys.argv[1:],
        ' && '.join(attach_commands) + " && sudo -k && bash -i ; " + ' && '.join(detach_commands)
    ]
    subprocess.run(ssh_cmd)

    # Cleanup
    unbind_and_stop_container(bound_ids)


if __name__ == "__main__":
    main()
