#!/bin/bash -e
# Neil Williams, reddit

# configuration that doesn't change much
REDDIT_REPO=git://github.com/reddit/reddit.git
I18N_REPO=git://github.com/reddit/reddit-i18n.git
APTITUDE_OPTIONS="-y" # limit bandwidth: -o Acquire::http::Dl-Limit=100"

# don't blunder on if an error occurs
set -e

# validate some assumptions of the script
if [ $(id -u) -ne 0 ]; then
    echo "ERROR: Must be run with root privileges."
    exit 1
fi

source /etc/lsb-release
if [ "$DISTRIB_ID" != "Ubuntu" ]; then
    echo "ERROR: Unknown distribution $DISTRIB_ID. Only Ubuntu is supported."
    exit 1
fi

if [ "$DISTRIB_RELEASE" != "11.04" ]; then
    echo "ERROR: Only Ubuntu 11.04 is supported."
    exit 1
fi

# get configured
echo "Welcome to the reddit install script!"

# until the scripts are better, just use "reddit"
REDDIT_USER=reddit
REDDIT_HOME=/home/$REDDIT_USER

echo "Beginning installation. This may take a while..."
echo

# create the user if non-existent
if ! id $REDDIT_USER > /dev/null; then
    adduser --system $REDDIT_USER
fi

# add some external ppas for packages
DEBIAN_FRONTEND=noninteractive apt-get install $APTITUDE_OPTIONS aptitude python-software-properties
apt-add-repository ppa:reddit/ppa

# pin the ppa
cat <<HERE > /etc/apt/preferences.d/reddit
Package: *
Pin: release o=LP-PPA-reddit
Pin-Priority: 600
HERE

# grab the new ppas' package listings
aptitude update

# install prerequisites
DEBIAN_FRONTEND=noninteractive aptitude install $APTITUDE_OPTIONS python-dev python-setuptools python-imaging python-pycaptcha python-mako python-nose python-decorator python-formencode python-pastescript python-beaker python-webhelpers python-amqplib python-pylibmc python-pycountry python-psycopg2 python-cssutils python-beautifulsoup python-sqlalchemy cython python-pybabel python-tz python-boto python-lxml python-pylons python-pycassa python-recaptcha gettext make optipng uwsgi uwsgi-core uwsgi-plugin-python nginx git-core python-profiler memcached postgresql postgresql-client curl daemontools daemontools-run rabbitmq-server cassandra python-bcrypt python-snudown python-python-statsd

# grab the source
if [ ! -d $REDDIT_HOME ]; then
    mkdir $REDDIT_HOME
    chown $REDDIT_USER $REDDIT_HOME
fi

cd $REDDIT_HOME

if [ ! -d $REDDIT_HOME/reddit ]; then
    sudo -u $REDDIT_USER git clone $REDDIT_REPO
fi

if [ ! -d $REDDIT_HOME/reddit-i18n ]; then
    sudo -u $REDDIT_USER git clone $I18N_REPO
fi

# wait a bit to make sure all the servers come up
sleep 30

# configure cassandra
if ! echo | cassandra-cli -h localhost -k reddit > /dev/null 2>&1; then
    echo "create keyspace reddit;" | cassandra-cli -h localhost -B
fi

echo "create column family permacache with column_type = 'Standard' and comparator = 'BytesType';" | cassandra-cli -B -h localhost -k reddit || true

# set up postgres
IS_DATABASE_CREATED=$(sudo -u postgres psql -t -c "SELECT COUNT(1) FROM pg_catalog.pg_database WHERE datname = 'reddit';")

if [ $IS_DATABASE_CREATED -ne 1 ]; then
    cat <<PGSCRIPT | sudo -u postgres psql
CREATE DATABASE reddit WITH ENCODING = 'utf8';
CREATE USER reddit WITH PASSWORD 'password';
PGSCRIPT
fi

sudo -u postgres psql reddit < $REDDIT_HOME/reddit/sql/functions.sql

# set up rabbitmq
if ! rabbitmqctl list_vhosts | egrep "^/$"
then
    rabbitmqctl add_vhost /
fi

if ! rabbitmqctl list_users | egrep "^reddit"
then
    rabbitmqctl add_user reddit reddit
fi

rabbitmqctl set_permissions -p / reddit ".*" ".*" ".*"

# run the reddit setup script
cd $REDDIT_HOME/reddit/r2
sudo -u $REDDIT_USER make pyx # generate the .c files from .pyx
sudo -u $REDDIT_USER python setup.py build
python setup.py develop

# run the i18n setup script
cd $REDDIT_HOME/reddit-i18n/
sudo -u $REDDIT_USER python setup.py build
python setup.py develop
sudo -u $REDDIT_USER make

# do the r2 build after languages are installed
cd $REDDIT_HOME/reddit/r2
sudo -u $REDDIT_USER make

# install the daemontools runscripts
cd $REDDIT_HOME/reddit/r2

if [ ! -f development.update ]; then
    cat > development.update <<UPDATE
[DEFAULT]
debug = true

disable_ads = true
disable_captcha = true
disable_ratelimit = true

page_cache_time = 0

set debug = true
UPDATE
    chown $REDDIT_USER development.update
    sudo -u $REDDIT_USER make ini
fi

if [ ! -L run.ini ]; then
    sudo -u $REDDIT_USER ln -s development.ini run.ini
fi

ln -s $REDDIT_HOME/reddit/srv/{comments_q,commentstree_q,scraper_q,vote_link_q,vote_comment_q} /etc/service/ || true
/sbin/start svscan || true

# set up uwsgi
cat >/etc/uwsgi/apps-available/reddit.ini <<UWSGI
[uwsgi]
; master / uwsgi protocol configuration
plugins = python
master = true
master-as-root = true
vacuum = true
limit-post = 512000
buffer-size = 8096
uid = $REDDIT_USER
chdir = $REDDIT_HOME/reddit/r2
socket = /tmp/reddit.sock
chmod-socket = 666

; worker configuration
lazy = true
max-requests = 10000
processes = 1

; app configuration
paste = config:$REDDIT_HOME/reddit/r2/run.ini
UWSGI

if [ ! -L /etc/uwsgi/apps-enabled/reddit.ini ]; then
    ln -s /etc/uwsgi/apps-available/reddit.ini /etc/uwsgi/apps-enabled/reddit.ini
fi

/etc/init.d/uwsgi start

# set up nginx
cat >/etc/nginx/sites-available/reddit <<NGINX
include uwsgi_params;
uwsgi_param SCRIPT_NAME "";

server {
    listen 8000;

    location / {
        uwsgi_pass unix:///tmp/reddit.sock;

        gzip on;
        gzip_vary on;
        gzip_proxied any;
        gzip_min_length 100;
        gzip_comp_level 4;
        gzip_types text/plain text/css text/javascript text/xml application/json text/csv application/x-javascript application/xml application/xml+rss;
    }
}
NGINX

if [ ! -L /etc/nginx/sites-enabled/reddit ]; then
    ln -s /etc/nginx/sites-available/reddit /etc/nginx/sites-enabled/reddit
fi
/etc/init.d/nginx restart

# install the crontab
CRONTAB=$(mktemp)

crontab -u $REDDIT_USER -l > $CRONTAB || true

cat >>$CRONTAB <<CRON
# m h  dom mon dow   command
*/5   *   *   *   *    $REDDIT_HOME/reddit/scripts/rising.sh
*/4   *   *   *   *    $REDDIT_HOME/reddit/scripts/send_mail.sh
*/3   *   *   *   *    $REDDIT_HOME/reddit/scripts/broken_things.sh
1     *   *   *   *    $REDDIT_HOME/reddit/scripts/update_promos.sh
0    23   *   *   *    $REDDIT_HOME/reddit/scripts/update_reddits.sh
30   23   *   *   *    $REDDIT_HOME/reddit/scripts/update_sr_names.sh
CRON
crontab -u $REDDIT_USER $CRONTAB
rm $CRONTAB

# done!
cd $REDDIT_HOME
echo "Done installing reddit!"
