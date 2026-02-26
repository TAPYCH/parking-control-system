import requests
import logging

logger = logging.getLogger("Hardware")

class RelayFactory:
    @staticmethod
    def send_command(gate_config: dict, channel: int, action: int, log_callback):
        relay_type = gate_config.get("relay_type", "dingtian")
        ip = gate_config.get("relay_ip")
        if not ip: return
        
        try:
            url = ""
            # 1. Dingtian (Китайский стандарт) - action: 0, 1, 2(pulse)
            if relay_type == "dingtian":
                url = f"http://{ip}/control/relay.cgi?relay={channel-1}&action={action}"
            
            # 2. Rodos / Laurent (ККМ)
            elif relay_type == "rodos":
                url = f"http://{ip}/pwr.cgi?p={channel}&s={action}"
            
            # 3. Kernel (Laurent-2/Логика)
            elif relay_type == "kernel":
                cmd = "1" if action > 0 else "0"
                url = f"http://{ip}/sec/?pt={channel}&cmd={cmd}"
                
            # 4. Shelly IoT (Умный дом)
            elif relay_type == "shelly":
                cmd = "on" if action > 0 else "off"
                url = f"http://{ip}/relay/{channel-1}?turn={cmd}"

            # 5. Sonoff (DIY mode)
            elif relay_type == "sonoff":
                cmd = "on" if action > 0 else "off"
                url = f"http://{ip}:8081/zeroconf/switch"
                requests.post(url, json={"deviceid": "", "data": {"switch": cmd}}, timeout=0.5)
                return

            if url: requests.get(url, timeout=0.5)
            
        except Exception as e:
            logger.error(f"❌ Ошибка реле {ip} ({relay_type}): {e}")
            log_callback("ERROR", f"Оборудование недоступно: {ip} [{relay_type}]")

class HardwareController:
    @staticmethod
    def open_barrier(gate_id: str, gate_config: dict, log_callback):
        RelayFactory.send_command(gate_config, gate_config["ch_green"], 1, log_callback)
        RelayFactory.send_command(gate_config, gate_config["ch_red"], 0, log_callback)
        RelayFactory.send_command(gate_config, gate_config["ch_barrier"], 2, log_callback) # 2 - pulse/on

    @staticmethod
    def close_barrier(gate_id: str, gate_config: dict, log_callback):
        RelayFactory.send_command(gate_config, gate_config["ch_green"], 0, log_callback)
        RelayFactory.send_command(gate_config, gate_config["ch_red"], 1, log_callback)