# Description

This application is a helper to perform HA with syslog-ng. The biggest problem when running multiple nodes is that usually UDP endpoint is unique (in my case handled using Corosync/Pacemaker and floating IP address), so backup server do not receive logs messages, only active one.

The solution described here uses a local RabbitMQ server on each Syslog-NG server to buffer all messages received on UDP/514 into a message queue for each backup server, then this small Python consumer will get logs from RabbitMQ and craft them to publish them into local Syslog-NG instance (UDP/515, to avoid replication loop), so your filtering rules based on source IP addresses still work.

# RabbitMQ config

I won't describe RabbitMQ server installation as it's pretty straight forward but once it's created, you need to login into admin interface at http://server:15672 and create a syslog-ng user on default virtual host (/) and you need to give this user all permissions on this vhost (or you can restrict with regex if you need, I assume RabbitMQ is dedicated to this task here).

Then you need to create a durable direct exchange named "syslog-ng" and a durable queue named "syslog-ng-buffer-for-server2" (or server1, on secondary server) then bing them together using default empty routing key.

Perform this operation on both nodes (could me more than two but in this case you will have to adapt Systemd configuration later).

# Syslog-ng config

Here is a small snippet of Syslog-NG configuration running on server1:

```
source s_net {
	udp(port(514));
};

source s_net_peer {
       # UDP on a different port to receive message from peer replica and avoid infinite replication loop
       udp(port(515));
};

# Duplicate all message received (on the shared IP) into a local RabbitMQ server
# So peer can consume this queue and also receive all messages
# 
# Don't forget to create disk-buffer folder (used if RabbitMQ server is unreachable)
# mkdir -p /srv/log/syslog-ng/disk-buffer/local-amqp
#
# And then create proper user/password using RabbitMQ admin interface on http://127.0.0.1:15672
# You will also need to create exchange and bind it to a queue that will be consumed by the peer
# syslog-ng server
# 
# I decided to make the syslog-ng admin with all permisions on / virtual host as it's intended
# to be used only for this purpose
#
# I cannot use exchange-declare(yes) because according to documentation I understand that used
# with persistent(yes) it should create a durable exchange, but this is not working
#
# AMQ message will contains parsed message in headers and raw message as bytes in body
# 
destination d_amqp {
    amqp(
        vhost("/")
        host("127.0.0.1")
        port(5672)
        username("syslog-ng")
        password("password")
        persistent(yes)
        exchange("syslog-ng")
        exchange-type("direct")
        exchange-declare(no)
        routing-key("")
        body("<$PRI>$DATE $HOST $MSGHDR$MESSAGE\n")
        value-pairs(
            scope("selected-macros" "nv-pairs" "sdata")
        )
        disk-buffer(
            mem-buf-size(163840000)
            disk-buf-size(1638400000)
            reliable(yes)
            dir("/srv/log/syslog-ng/disk-buffer/local-amqp")
        )
    );
};

log { source(s_net); destination(d_amqp); };

destination net_log { file("/var/log/net.log" owner("root") group("adm") perm(0644)); };

log { source(s_net); source(s_net_peer); destination(net_log); };
```

I later modified the configuration to separate legacy BSD messages from new IETF ones, using different RabbitMQ routing key and different ports.

# RabbitMQ consumer

This Python application must be installed on each Syslog-NG server and will consume buffered message from other node.

Dependencies are really minimal, it uses scapy to craft UDP packets (`apt install python3-scapy`) and a recent pika package (sadly Debian one is absolutely outdated and will not be sufficient, it should be quite easy to port the code to this old version if you are interestend in).

```
git clone https://github.com/eLvErDe/syslog-ng-peer-amq-consumer.git /opt/syslog-ng-peer-amq-consumer

cp -v systemd/service /etc/systemd/system/syslog-ng-peer-amq-consumer.service
cp -v systemd/default /etc/default/syslog-ng-peer-amq-consumer

systemctl daemon-reload
```

Then edit `/etc/default/syslog-ng-peer-amq-consumer` to configure proper credentials/ip/queue name for peer server and enable/start the service.

```
systemctl enable syslog-ng-peer-amq-consumer.service
systemctl start syslog-ng-peer-amq-consumer.service
```

Repeat this operation on the second server.

Message are now being consumed from remote Syslog-NG peer RabbitMQ server and pushed into local Syslog-NG on port UDP/515.

Please not that the systemd service file is configured so this script **CANNOT** run if syslog-ng is stopped or crashs (because otherwise you're pushing logs to a non-exsiting endpoint and losing them) and it will be started after syslog-ng.


# Bonus script

Another script is available to automatically create missing folder using in dir("") or "file("") Syslog-NG config directives.

It can be integrated as pre start/reload script for Syslog-NG using systemd:

```
mkdir -p /etc/systemd/system/syslog-ng.service.d/
echo "[Service]" > /etc/systemd/system/syslog-ng.service.d/create-missing-folders.conf
echo "Environment=NO_LOGS_TS=1" >> /etc/systemd/system/syslog-ng.service.d/create-missing-folders.conf
echo "ExecStartPre=/usr/bin/python3 /opt/syslog-ng-peer-amq-consumer/syslog_ng_create_missing_folders.py --syslog-ng-conf-dir /etc/syslog-ng" >> /etc/systemd/system/syslog-ng.service.d/create-missing-folders.conf
echo "ExecReload=/usr/bin/python3 /opt/syslog-ng-peer-amq-consumer/syslog_ng_create_missing_folders.py --syslog-ng-conf-dir /etc/syslog-ng" >> /etc/systemd/system/syslog-ng.service.d/create-missing-folders.conf
systemctl daemon-reload
systemctl reload syslog-ng
```

Newly created folders will be logged to stdout which is sent to syslog using systemd-journald (at least on recent Debians).

You don't have to handle this anymore, each time the service is restarted or reloaded, missing folders will be re-created.
