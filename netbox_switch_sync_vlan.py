import os
import re
import json
import logging
import requests
import time
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv
from pyats.topology import Device, loader
from requests.packages.urllib3.exceptions import InsecureRequestWarning

# --- CONFIGURATION ---
# Load variables from your .env file
load_dotenv()

# Set the name of the switch you want to demo
DEMO_SWITCH_NAME = "isr1100.myrouter" # <--- IMPORTANT: Change this to your switch's name in NetBox
NETBOX_SITE_SLUG = "new_york"        # <--- IMPORTANT: Change this to your site's slug in NetBox

# --- LOGGING SETUP ---
# Set up logging so you can see the script's progress in your terminal
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("NetBoxSyncDemo")

# Disable insecure request warnings if you're using self-signed certs
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# ==============================================================================
# SECTION 1: NETBOX CLIENT (From your pyats_mcp_server.py)
# This class handles all communication with the NetBox API.
# ==============================================================================

class NetBoxRestClient:
    """Minimal NetBox client implementation using the REST API."""
    def __init__(self, url: str, token: str, verify_ssl: bool = False):
        self.base_url = url.rstrip('/')
        self.api_url = f"{self.base_url}/api"
        self.token = token
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Token {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json; indent=4',
        })
        logger.info(f"NetBoxRestClient initialized for URL: {self.base_url}")

    def _build_url(self, endpoint: str, id: Optional[int] = None) -> str:
        endpoint = endpoint.strip('/')
        if id is not None:
            return f"{self.api_url}/{endpoint}/{id}/"
        return f"{self.api_url}/{endpoint}/"

    def get(self, endpoint: str, id: Optional[int] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._build_url(endpoint, id)
        try:
            response = self.session.get(url, params=params, verify=self.verify_ssl, timeout=60)
            response.raise_for_status() # Raises HTTPError for 4xx/5xx
            
            data = response.json()
            # Handle paginated results automatically
            if "results" in data and isinstance(data["results"], list) and data.get("next"):
                all_results = data["results"]
                next_page_url = data.get("next")
                while next_page_url:
                    logger.info("Fetching next page...")
                    response = self.session.get(next_page_url, verify=self.verify_ssl, timeout=60)
                    response.raise_for_status()
                    page_data = response.json()
                    all_results.extend(page_data.get("results", []))
                    next_page_url = page_data.get("next")
                data["results"] = all_results
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"NetBox API Error requesting {url}: {e}")
            raise ConnectionError(f"NetBox API Error contacting {url}: {e}") from e

    def patch(self, endpoint: str, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Performs a BULK PATCH request."""
        url = self._build_url(endpoint)
        logger.debug(f"NetBox BULK PATCH Request: URL={url}, Data Count={len(data)}")
        try:
            response = self.session.patch(url, json=data, verify=self.verify_ssl, timeout=300)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"NetBox API PATCH Error ({url}): {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"NetBox API Error Response: {e.response.text}")
            raise ConnectionError(f"NetBox API Error updating {url}: {e}") from e

# ==============================================================================
# SECTION 2: PYATS CONNECTION (From your pyats_mcp_server.py)
# These functions handle connecting to the switch.
# ==============================================================================

def _get_platform_os(nb_device: dict) -> tuple[Optional[str], str]:
    """Determines platform slug and pyATS OS type from NetBox device data."""
    platform_info = nb_device.get("platform")
    os_type = 'generic'
    platform_slug = None
    if platform_info and isinstance(platform_info, dict):
        platform_slug = platform_info.get("slug", "").lower()
        platform_name = platform_info.get("name", "").lower()

        if 'iosxe' in platform_slug or 'ios-xe' in platform_slug or 'ios xe' in platform_name:
            os_type = 'iosxe'
        elif 'ios' in platform_slug or 'ios' in platform_name:
            os_type = 'ios'
        elif 'nxos' in platform_slug or 'nx-os' in platform_slug:
            os_type = 'nxos'
        # ... (add other OS types as needed) ...
    
    if os_type == 'generic':
        logger.warning(f"Platform info missing or unmapped for device '{nb_device.get('name')}'. Using OS='generic'.")
    
    original_platform_slug = platform_info.get("slug") if platform_info else None
    return original_platform_slug, os_type

def _disconnect_device(device: Optional[Device]):
    """Helper to safely disconnect a pyATS Device object."""
    if device and isinstance(device, Device) and hasattr(device, 'is_connected') and device.is_connected():
        logger.info(f"Disconnecting from {device.name}...")
        try:
            device.disconnect()
            logger.info(f"Disconnected from {device.name}")
        except Exception as e:
            logger.warning(f"Error during disconnect from {device.name}: {e}")

def _get_device(device_name: str, netbox_client: NetBoxRestClient) -> Device:
    """
    Fetches device details from NetBox, uses credentials from .env,
    and connects with pyATS.
    """
    logger.info(f"Getting device info for '{device_name}' from NetBox...")
    
    # 1. Find Device in NetBox
    nb_devices_response = netbox_client.get("dcim/devices/", params={"name": device_name})
    if not nb_devices_response.get("results"):
        raise ValueError(f"Device '{device_name}' not found in NetBox")
    nb_device = nb_devices_response["results"][0]
    
    primary_ip_info = nb_device.get("primary_ip")
    if not primary_ip_info or not primary_ip_info.get("address"):
        raise ValueError(f"Missing primary IP for device '{device_name}' in NetBox")
    primary_ip = primary_ip_info["address"].split('/')[0]
    
    _platform_slug, os_type = _get_platform_os(nb_device)
    
    # 2. Get Credentials from .env
    username = os.getenv("DEFAULT_SSH_USERNAME")
    password = os.getenv("DEFAULT_SSH_PASSWORD")
    if not username or not password:
        raise ValueError("DEFAULT_SSH_USERNAME or DEFAULT_SSH_PASSWORD not set in .env")

    # 3. Construct Testbed Dictionary
    testbed_data = {
        'devices': {
            device_name: {
                'os': os_type,
                'platform': _platform_slug if _platform_slug else os_type,
                'type': 'router', # or 'switch', doesn't matter much for this
                'connections': {
                    'default': {
                        'protocol': 'ssh',
                        'ip': primary_ip,
                        'port': 22,
                    }
                },
                'credentials': {
                    'default': {
                        'username': username,
                        'password': password
                    }
                }
            }
        }
    }

    # 4. Load and Connect
    tb = loader.load(testbed_data)
    pyats_device_obj = tb.devices[device_name]
    
    logger.info(f"Attempting SSH connection to {primary_ip}...")
    pyats_device_obj.connect(
        log_stdout=False,
        connection_timeout=20,
        learn_hostname=True
    )
    logger.info(f"Successfully connected to device '{device_name}'")
    return pyats_device_obj

# ==============================================================================
# SECTION 3: CONFIG PARSER (From your collector.py)
# These functions understand the "show running-config" output.
# ==============================================================================

def _normalize_iface_name(iface_name: str) -> str:
    """A shared helper to consistently shorten interface names."""
    if not isinstance(iface_name, str):
        return ""
    
    name = iface_name.lower().strip().replace(" ", "")
    name = name.replace("gigabitethernet", "gi")
    name = name.replace("tengigabitethernet", "te")
    name = name.replace("fastethernet", "fa")
    name = name.replace("port-channel", "po")
    
    if name.startswith("gi"): return "Gi" + name[2:]
    if name.startswith("te"): return "Te" + name[2:]
    if name.startswith("fa"): return "Fa" + name[2:]
    if name.startswith("po"): return "Po" + name[2:]
    
    return iface_name # Return original if no match

def parse_interface_config(config_text: str) -> Dict[str, Dict]:
    """
    Parses a 'show running-config' output by splitting the config into blocks
    using the '!' delimiter.
    """
    interfaces = {}
    config_blocks = config_text.split('!')

    for block in config_blocks:
        lines = block.strip().splitlines()
        if not lines or not lines[0].lower().startswith('interface '):
            continue

        if_name_raw = lines[0].replace("interface ", "").strip()
        if_name = _normalize_iface_name(if_name_raw)
        interfaces[if_name] = {}

        # Default to enabled, set to false if 'shutdown' is found
        interfaces[if_name]['enabled'] = True
        if re.search(r"^\s*shutdown\b", block, re.MULTILINE):
            interfaces[if_name]['enabled'] = False
        
        if if_name.lower().startswith('po'):
            interfaces[if_name]['is_port_channel_parent'] = True

        config_str = "\n".join(lines[1:])
        
        desc_match = re.search(r"^\s*description (.*)", config_str, re.MULTILINE)
        if desc_match:
            interfaces[if_name]['description'] = desc_match.group(1).strip()
        
        voice_vlan_match = re.search(r"^\s*switchport voice vlan (\d+)", config_str, re.MULTILINE)
        if voice_vlan_match:
            interfaces[if_name]['voice_vlan'] = voice_vlan_match.group(1)

        channel_group_match = re.search(r"^\s*channel-group (\d+) mode", config_str, re.MULTILINE)
        if channel_group_match:
            interfaces[if_name]['channel_group'] = channel_group_match.group(1)

        # Mode parsing logic
        mode = None
        if re.search(r"^\s*switchport mode trunk\s*$", config_str, re.MULTILINE):
            mode = 'trunk'
        elif re.search(r"^\s*switchport mode access\s*$", config_str, re.MULTILINE):
            mode = 'access'
        elif re.search(r"switchport trunk allowed vlan", config_str):
            mode = 'trunk'
        elif "switchport access vlan" in config_str:
            mode = 'access'
        else:
            mode = 'access' # Default
            
        interfaces[if_name]['mode'] = mode

        if mode == 'trunk':
            native_vlan_match = re.search(r"^\s*switchport trunk native vlan (\d+)", config_str, re.MULTILINE)
            if native_vlan_match:
                interfaces[if_name]['native_vlan'] = int(native_vlan_match.group(1))

            vlan_list = []
            allowed_vlan_matches = re.findall(r"switchport trunk allowed vlan (?:add )?([\d,-]+)", config_str)
            for vlan_match in allowed_vlan_matches:
                for part in vlan_match.split(','):
                    if '-' in part:
                        start, end = map(int, part.split('-'))
                        vlan_list.extend(range(start, end + 1))
                    else:
                        vlan_list.append(int(part))
            
            if vlan_list:
                interfaces[if_name]['allowed_vlans'] = sorted(list(set(vlan_list)))
            elif not allowed_vlan_matches:
                interfaces[if_name]['allowed_vlans'] = 'ALL'

        elif mode == 'access':
            vlan_match = re.search(r"^\s*switchport access vlan (\d+)", config_str, re.MULTILINE)
            if vlan_match:
                interfaces[if_name]['access_vlan'] = int(vlan_match.group(1))

    return interfaces

# ==============================================================================
# SECTION 4: MAIN SCRIPT LOGIC
# This is the "main" function that runs our entire demo.
# ==============================================================================

def main():
    """
    Main function to run the complete sync process for a single switch.
    """
    device = None
    all_updates = []
    
    try:
        # --- 1. Connect to NetBox ---
        logger.info("Connecting to NetBox...")
        netbox_client = NetBoxRestClient(
            url=os.getenv("NETBOX_URL"),
            token=os.getenv("NETBOX_TOKEN")
        )

        # --- 2. Build VLAN Map ---
        logger.info(f"Fetching VLAN map for site '{NETBOX_SITE_SLUG}'...")
        all_vlans_data = netbox_client.get("ipam/vlans/", params={"site_slug": NETBOX_SITE_SLUG})
        
        vlan_site_map = {} # This is our "translation" map
        for vlan in all_vlans_data.get("results", []):
            vid = vlan.get('vid')
            vlan_id = vlan.get('id')
            if vid is not None and vlan_id is not None:
                vlan_site_map[vid] = vlan_id
        logger.info(f"Built VLAN map with {len(vlan_site_map)} entries.")

        # --- 3. Get Device and Interface Info from NetBox ---
        logger.info(f"Fetching device and interface data for '{DEMO_SWITCH_NAME}' from NetBox...")
        nb_device_data = netbox_client.get("dcim/devices/", params={"name": DEMO_SWITCH_NAME})
        if not nb_device_data.get("results"):
            raise ValueError(f"Could not find device '{DEMO_SWITCH_NAME}' in NetBox.")
            
        device_id = nb_device_data["results"][0]["id"]
        site_id = nb_device_data["results"][0]["site"]["id"] # We use this for VLAN lookups
        
        nb_interfaces_data = netbox_client.get("dcim/interfaces/", params={"device_id": device_id})
        nb_interfaces = nb_interfaces_data.get("results", [])
        logger.info(f"Found {len(nb_interfaces)} interfaces for '{DEMO_SWITCH_NAME}' in NetBox.")

        # --- 4. Connect to the Live Switch ---
        logger.info(f"Connecting to live switch '{DEMO_SWITCH_NAME}'...")
        device = _get_device(DEMO_SWITCH_NAME, netbox_client)
        
        # --- 5. Get Live Config ---
        logger.info(f"Fetching 'show running-config all' from {DEMO_SWITCH_NAME}...")
        live_config_text = device.execute("show running-config all", timeout=300)
        
        # --- 6. Parse Live Config ---
        logger.info("Parsing live configuration...")
        parsed_live_interfaces = parse_interface_config(live_config_text)
        logger.info(f"Parsed {len(parsed_live_interfaces)} interfaces from live config.")

        # --- 7. Compare and Find Updates (The Core Logic) ---
        logger.info("Comparing live config to NetBox data and building update list...")
        
        for nb_iface in nb_interfaces:
            iface_name = _normalize_iface_name(nb_iface['name'])
            live_iface = parsed_live_interfaces.get(iface_name)

            if not live_iface:
                logger.warning(f"Interface '{iface_name}' in NetBox but not in live config. Skipping.")
                continue

            payload = {'id': nb_iface['id']}
            has_changed = False

            # Compare Enabled Status
            live_enabled = live_iface.get('enabled', True)
            if live_enabled != nb_iface.get('enabled'):
                payload['enabled'] = live_enabled
                has_changed = True

            # Compare Description
            live_desc = live_iface.get('description')
            if live_desc is not None and live_desc != nb_iface.get('description', ''):
                payload['description'] = live_desc
                has_changed = True
            
            # --- VLAN and Mode Update Logic ---
            if 'channel_group' not in live_iface:
                live_mode = live_iface.get('mode')
                mapped_live_mode = 'tagged' if live_mode == 'trunk' else 'access' if live_mode == 'access' else None
                current_netbox_mode = (nb_iface.get('mode') or {}).get('value')

                if mapped_live_mode and mapped_live_mode != current_netbox_mode:
                    payload['mode'] = mapped_live_mode
                    has_changed = True

                # Access Port Logic
                if mapped_live_mode == 'access':
                    live_vlan_vid = live_iface.get('access_vlan') or 1
                    final_untagged_id = vlan_site_map.get(live_vlan_vid) # Simplified to site map

                    if final_untagged_id is not None and final_untagged_id != (nb_iface.get('untagged_vlan') or {}).get('id'):
                        payload['untagged_vlan'] = final_untagged_id
                        payload['tagged_vlans'] = [] # Clear tagged VLANs on an access port
                        has_changed = True

                # Trunk Port Logic
                elif mapped_live_mode == 'tagged':
                    # Native VLAN
                    live_native_vid = live_iface.get('native_vlan')
                    final_native_id = vlan_site_map.get(live_native_vid) if live_native_vid else None
                    current_native_id = (nb_iface.get('untagged_vlan') or {}).get('id')
                    
                    if final_native_id != current_native_id:
                        payload['untagged_vlan'] = final_native_id
                        has_changed = True

                    # Tagged VLANs
                    live_allowed_vids = live_iface.get('allowed_vlans', [])
                    current_tagged_ids = {v['id'] for v in nb_iface.get('tagged_vlans', [])}
                    final_tagged_ids = set()
                    
                    if live_allowed_vids != 'ALL':
                        for vid in live_allowed_vids:
                            if vid == live_native_vid: continue # Native is untagged
                            vlan_id = vlan_site_map.get(vid)
                            if vlan_id:
                                final_tagged_ids.add(vlan_id)
                    
                    if final_tagged_ids != current_tagged_ids:
                        payload['tagged_vlans'] = list(final_tagged_ids)
                        has_changed = True

            if has_changed:
                logger.info(f"Update found for {iface_name}: {list(payload.keys())[1:]}")
                all_updates.append(payload)

        # --- 8. Push Updates to NetBox ---
        if all_updates:
            logger.info(f"Found {len(all_updates)} total updates. Sending batch update to NetBox...")
            netbox_client.patch("dcim/interfaces/", data=all_updates)
            logger.info("✅✅✅ Sync Complete! NetBox is now up-to-date. ✅✅✅")
        else:
            logger.info("✅ Sync Complete. No updates were needed.")

    except Exception as e:
        logger.error(f"❌ An error occurred during the sync process: {e}", exc_info=True)
    finally:
        # --- 9. Disconnect ---
        if device:
            _disconnect_device(device)
            logger.info("Process finished.")

if __name__ == "__main__":
    main()
