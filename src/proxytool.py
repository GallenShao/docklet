#!/usr/bin/python3

import requests, json
import env
from log import logger

proxy_api_port = env.getenv("PROXY_API_PORT")
proxy_control="http://localhost:"+ str(proxy_api_port) +"/api/routes"

def get_portal_domain():
    url = env.getenv("PORTAL_URL")
    if url.startswith("http://"):
        url = url[7:]
    if url.startswith("https://"):
        url = url[8:]
    url = url.split('/')[0].split(':')[0]
    return url
portal_url_host = get_portal_domain()
portal_host_list = []
portal_host_list.append(portal_url_host)
portal_host_list.append(env.getenv("PUBLIC_IP"))
portal_host_list.append("0.0.0.0")
portal_host_list.append("127.0.0.1")
portal_host_list = list(set(portal_host_list))

def get_routes():
    try:
        resp = requests.get(proxy_control)
    except:
        return [False, 'Connect Failed']
    return [True, resp.json()]

def set_route(path, target, subdomain=''):
    if subdomain != '':
        route_path = '/' + (subdomain + '.' + portal_url_host + '/' + path.strip('/')).strip('/')
        try:
            resp = requests.post(proxy_control + route_path.lower(), data=json.dumps({'target':target}))
        except:
            return [False, 'Connect Failed']
    else:
        for host in portal_host_list:
            route_path = '/' + (host + '/' + path.strip('/')).strip('/')
            logger.info('[set_route] %s -> %s' % (route_path, target))
            if route_path=='' or target=='':
                return [False, 'input not valid']
            try:
                resp = requests.post(proxy_control + route_path, data=json.dumps({'target':target}))
            except:
                return [False, 'Connect Failed']
    return [True, 'set ok']
    
def delete_route(path, subdomain=''):
    if subdomain != '':
        route_path = '/' + (subdomain + '.' + portal_url_host + '/' + path.strip('/')).strip('/')
        try:
            resp = requests.delete(proxy_control + route_path.lower())
        except:
            return [False, 'Connect Failed']
    else:
        for host in portal_host_list:
            route_path = '/' + (host + '/' + path.strip('/')).strip('/')
            logger.info('[delete_route] %s' % route_path)
            try:
                resp = requests.delete(proxy_control + route_path)
            except:
                return [False, 'Connect Failed']
    # if exist and delete, status_code=204, if not exist, status_code=404
    return [True, 'delete ok']
