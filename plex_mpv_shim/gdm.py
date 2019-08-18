"""
PlexGDM.py - Version 0.3

This class implements the Plex GDM (G'Day Mate) protocol to discover
local Plex Media Servers.  Also allow client registration into all local
media servers.


This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
MA 02110-1301, USA.
"""

__author__ = 'DHJ (hippojay) <plex@h-jay.com>'

import socket
import struct
import sys
import re
import threading
import time
import urllib.request, urllib.error, urllib.parse
from .conf import settings

class PlexGDM:

    def __init__(self, debug=0):
        
        self.discover_message = b'M-SEARCH * HTTP/1.0'
        self.client_header = b'* HTTP/1.0'
        self.client_data = None
        self.client_id = None
        
        self._multicast_address = '239.0.0.250'
        self.discover_group = (self._multicast_address, 32414)
        self.client_register_group = (self._multicast_address, 32413)
        self.client_update_port = 32412

        self.server_list = []
        self.discovery_interval = 120
        
        self._discovery_is_running = False
        self._registration_is_running = False

        self.discovery_complete = False
        self.client_registered = False
        self.debug = debug

    def __printDebug(self, message, level=1):
        if self.debug >= level:
            print("PlexGDM: %s" % message)

    def clientDetails(self, c_id, c_name, c_port, c_product, c_version):
        capabilities = b"timeline,playback,navigation"
        if settings.enable_play_queue:
            capabilities = b"timeline,playback,navigation,playqueues"
        
        data = {
            b"Name":                  str(c_name).encode("utf-8"),
            b"Port":                  str(c_port).encode("utf-8"),
            b"Product":               str(c_product).encode("utf-8"),
            b"Content-Type":          b"plex/media-player",
            b"Protocol":              b"plex",
            b"Protocol-Version":      b"1",
            b"Protocol-Capabilities": capabilities,
            b"Version":               str(c_version).encode("utf-8"),
            b"Resource-Identifier":   str(c_id).encode("utf-8"),
            b"Device-Class":          b"HTPC"
        }
        
        self.client_data = b""
        for key, value in list(data.items()):
            self.client_data += b"%s: %s\n" % (key, value)
        self.client_data = self.client_data.strip()
        
        self.client_id = c_id
        
    def getClientDetails(self):
        if not self.client_data:
            self.__printDebug("Client data has not been initialised.  Please use PlexGDM.clientDetails()")

        return self.client_data

    def client_update (self):
        update_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        
        #Set socket reuse, may not work on all OSs.
        try:
            update_sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        except:
            pass
        
        #Attempt to bind to the socket to recieve and send data.  If we can;t do this, then we cannot send registration
        try:
            update_sock.bind(('0.0.0.0',self.client_update_port))
        except:
            self.__printDebug( "Error: Unable to bind to port [%s] - client will not be registered" % self.client_update_port, 0)
            return    
        
        update_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        status = update_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(self._multicast_address) + socket.inet_aton('0.0.0.0'))
        update_sock.setblocking(0)
        self.__printDebug("Sending registration data: HELLO %s\n%s" % (self.client_header, self.client_data), 3)
        
        #Send initial client registration
        try:
            update_sock.sendto(b"HELLO %s\n%s" % (self.client_header, self.client_data), self.client_register_group)
        except:
            self.__printDebug( "Error: Unable to send registeration message" , 0)
        
        #Now, listen for client discovery reguests and respond.
        while self._registration_is_running:
            try:
                data, addr = update_sock.recvfrom(1024)
                self.__printDebug("Recieved UDP packet from [%s] containing [%s]" % (addr, data.strip()), 3)
            except socket.error as e:
                pass
            else:
                if b"M-SEARCH * HTTP/1." in data:
                    self.__printDebug("Detected client discovery request from %s.  Replying" % ( addr ,) , 2)
                    try:
                        update_sock.sendto(b"HTTP/1.0 200 OK\n%s" % self.client_data, addr)
                    except:
                        self.__printDebug( "Error: Unable to send client update message",0)
                    
                    self.__printDebug("Sending registration data: HTTP/1.0 200 OK\n%s" % (self.client_data), 3)
                    self.client_registered = True
            time.sleep(0.5)        

        self.__printDebug("Client Update loop stopped",1)
        
        #When we are finished, then send a final goodbye message to deregister cleanly.
        self.__printDebug("Sending registration data: BYE %s\n%s" % (self.client_header, self.client_data), 3)
        try:
            update_sock.sendto(b"BYE %s\n%s" % (self.client_header, self.client_data), self.client_register_group)
        except:
            self.__printDebug( "Error: Unable to send client update message" ,0)
                       
        self.client_registered = False
                           
    def check_client_registration(self):
        
        if self.client_registered and self.discovery_complete:
        
            if not self.server_list:
                self.__printDebug("Server list is empty. Unable to check",2)
                return False

            try:
                media_server=self.server_list[0]['server']
                media_port=self.server_list[0]['port']

                self.__printDebug("Checking server [%s] on port [%s]" % (media_server, media_port) ,2)                    
                f = urllib.request.urlopen('http://%s:%s/clients' % (media_server, media_port))
                client_result = f.read()
                if self.client_id in client_result:
                    self.__printDebug("Client registration successful",1)
                    self.__printDebug("Client data is: %s" % client_result, 3)
                    return True
                else:
                    self.__printDebug("Client registration not found",1)
                    self.__printDebug("Client data is: %s" % client_result, 3)
                   
            except:
                self.__printDebug("Unable to check status")
                pass
        
        return False
            
    def getServerList (self):
        return self.server_list
        
    def discover(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Set a timeout so the socket does not block indefinitely
        sock.settimeout(0.6)

        # Set the time-to-live for messages to 1 for local network
        ttl = struct.pack('b', 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)

        returnData = []
        try:
            # Send data to the multicast group
            self.__printDebug("Sending discovery messages: %s" % self.discover_message, 2)
            sent = sock.sendto(self.discover_message, self.discover_group)

            # Look for responses from all recipients
            while True:
                try:
                    data, server = sock.recvfrom(1024)
                    self.__printDebug("Received data from %s, %s" % server, 3)
                    self.__printDebug("Data received is:\n %s" % data, 3)
                    returnData.append( { 'from' : server,
                                         'data' : data } )
                except socket.timeout:
                    break
        finally:
            sock.close()

        self.discovery_complete = True

        discovered_servers = []

        if returnData:

            for response in returnData:
                update = { 'server' : response.get('from')[0] }

                #Check if we had a positive HTTP response                        
                if "200 OK" in response.get('data'):
            
                    for each in response.get('data').split('\n'):

                        update['discovery'] = "auto"
                        update['owned']='1'
                        update['master']= 1
                        update['role']='master'
                        update['class']=None
                    
                        if "Content-Type:" in each:
                            update['content-type'] = each.split(':')[1].strip()
                        elif "Resource-Identifier:" in each:
                            update['uuid'] = each.split(':')[1].strip()
                        elif "Name:" in each:
                            update['serverName'] = each.split(':')[1].strip()
                        elif "Port:" in each:
                            update['port'] = each.split(':')[1].strip()
                        elif "Updated-At:" in each:
                            update['updated'] = each.split(':')[1].strip()
                        elif "Version:" in each:
                            update['version'] = each.split(':')[1].strip()
                        elif "Server-Class:" in each:
                            update['class'] = each.split(':')[1].strip()

                discovered_servers.append(update)                    

        self.server_list = discovered_servers
        
        if not self.server_list:
            self.__printDebug("No servers have been discovered",1)
        else:
            self.__printDebug("Number of servers Discovered: %s" % len(self.server_list),1)
            for items in self.server_list:
                self.__printDebug("Server Discovered: %s" % items['serverName'] ,2)
                

    def setInterval(self, interval):
        self.discovery_interval = interval

    def stop_all(self):
        self.stop_discovery()
        self.stop_registration()

    def stop_discovery(self):
        if self._discovery_is_running:
            self.__printDebug("Discovery shutting down", 1)
            self._discovery_is_running = False
            self.discover_t.join()
            del self.discover_t
        else:
            self.__printDebug("Discovery not running", 1)

    def stop_registration(self):
        if self._registration_is_running:
            self.__printDebug("Registration shutting down", 1)
            self._registration_is_running = False
            self.register_t.join()
            del self.register_t
        else:
            self.__printDebug("Registration not running", 1)

    def run_discovery_loop(self):
        #Run initial discovery
        self.discover()

        discovery_count=0
        while self._discovery_is_running:
            discovery_count+=1
            if discovery_count > self.discovery_interval:
                self.discover()
                discovery_count=0
            time.sleep(1)

    def start_discovery(self, daemon = False):
        if not self._discovery_is_running:
            self.__printDebug("Discovery starting up", 1)
            self._discovery_is_running = True
            self.discover_t = threading.Thread(target=self.run_discovery_loop)
            self.discover_t.setDaemon(daemon)
            self.discover_t.start()
        else:
            self.__printDebug("Discovery already running", 1)
        
    def start_registration(self, daemon = False):
        if not self._registration_is_running:
            self.__printDebug("Registration starting up", 1)
            self._registration_is_running = True
            self.register_t = threading.Thread(target=self.client_update)
            self.register_t.setDaemon(daemon)
            self.register_t.start()
        else:
            self.__printDebug("Registration already running", 1)
             
    def start_all(self, daemon = False):
        self.start_discovery(daemon)
        self.start_registration(daemon)

gdm = PlexGDM()
