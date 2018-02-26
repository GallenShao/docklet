#!/bin/bash

MASTER_IP=0.0.0.0
MASTER_IPS=0.0.0.0@docklet
NGINX_PORT=8080
PROXY_PORT=8000
WEB_PORT=8888
NGINX_CONF=/etc/nginx
PORTAL_URL=http://0.0.0.0:8080

toolsdir=${0%/*}
DOCKLET_TOOLS=$(cd $toolsdir; pwd)
DOCKLET_HOME=${DOCKLET_TOOLS%/*}
DOCKLET_CONF=$DOCKLET_HOME/conf

. $DOCKLET_CONF/docklet.conf

if [[ ${PORTAL_URL} == http://* ]] || [[ ${PORTAL_URL} == https://* ]]
then
	DOMAIN=`echo ${PORTAL_URL} | cut -d: -f2 | cut -d/ -f3`
else
	DOMAIN=`echo ${PORTAL_URL} | cut -d: -f1 | cut -d/ -f3`
fi

NGINX_CONF=${NGINX_CONF}/sites-enabled

echo "copy nginx_docklet.conf to nginx config path..."
cp $DOCKLET_CONF/nginx_docklet.conf ${NGINX_CONF}/

while IFS=',' read -ra arr; do
	for item in "${arr[@]}"; do
		PROXY_NAME=`echo ${item} | cut -d@ -f2`
		PROXY_IP=`echo ${item} | cut -d@ -f1`
		cat $DOCKLET_CONF/nginx_proxy.conf | sed -e "s/%PROXY_NAME/${PROXY_NAME}/g" -e "s/%PROXY_IP/${PROXY_IP}/g" >> ${NGINX_CONF}/nginx_docklet.conf
	done
done <<< "$MASTER_IPS"

sed -i "s/%DOMAIN/${DOMAIN}/g" ${NGINX_CONF}/nginx_docklet.conf
sed -i "s/%MASTER_IP/${MASTER_IP}/g" ${NGINX_CONF}/nginx_docklet.conf
sed -i "s/%NGINX_PORT/${NGINX_PORT}/g" ${NGINX_CONF}/nginx_docklet.conf

sed -i "s/%PROXY_PORT/${PROXY_PORT}/g" ${NGINX_CONF}/nginx_docklet.conf
sed -i "s/%WEB_PORT/${WEB_PORT}/g" ${NGINX_CONF}/nginx_docklet.conf

if [ "${NGINX_PORT}" != "80" ]
then
  sed -i "s/\$host/\$host:\$server_port/g" ${NGINX_CONF}/nginx_docklet.conf
fi


echo "restart nginx..."
/etc/init.d/nginx restart
