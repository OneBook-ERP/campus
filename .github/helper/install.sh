#!/bin/bash
# Build a bench with campus and its required apps on a GitHub runner.
set -euo pipefail

cd ~ || exit

# Use a direct Ubuntu mirror instead of the slow Azure mirror.
sudo tee /etc/apt/apt-mirrors.txt >/dev/null <<'EOF'
https://archive.ubuntu.com/ubuntu
EOF

APT_OPTIONS=(
	-o Acquire::Retries=2
	-o Acquire::http::Timeout=20
	-o Acquire::https::Timeout=20
)

echo "::group::Install system dependencies"
sudo apt-get "${APT_OPTIONS[@]}" update
sudo env DEBIAN_FRONTEND=noninteractive \
	apt-get "${APT_OPTIONS[@]}" remove -y mysql-server mysql-client
sudo env DEBIAN_FRONTEND=noninteractive \
	apt-get "${APT_OPTIONS[@]}" install -y \
	libcups2-dev redis-server mariadb-client libmariadb-dev
echo "::endgroup::"

echo "::group::Install Bench"
python -m pip install --timeout 60 --retries 3 frappe-bench

frappebranch=${FRAPPE_BRANCH:-version-16}

git clone \
	"https://github.com/frappe/frappe" \
	--branch "$frappebranch" \
	--depth 1

bench init \
	--skip-assets \
	--frappe-path ~/frappe \
	--python "$(command -v python)" \
	frappe-bench
echo "::endgroup::"

mkdir -p ~/frappe-bench/sites/test_site
cp \
	"${GITHUB_WORKSPACE}/.github/helper/site_config.json" \
	~/frappe-bench/sites/test_site/

echo "::group::Configure database"
mariadb --host 127.0.0.1 --port 3306 -u root -proot <<'SQL'
SET GLOBAL character_set_server = 'utf8mb4';
SET GLOBAL collation_server = 'utf8mb4_unicode_ci';
CREATE USER 'test_frappe'@'localhost' IDENTIFIED BY 'test_frappe';
CREATE DATABASE test_frappe;
GRANT ALL PRIVILEGES ON `test_frappe`.* TO 'test_frappe'@'localhost';
FLUSH PRIVILEGES;
SQL
echo "::endgroup::"

install_wkhtmltopdf() {
	echo "Downloading wkhtmltopdf"

	wget \
		--timeout=30 \
		--tries=3 \
		--no-verbose \
		-O /tmp/wkhtmltox.tar.xz \
		https://github.com/frappe/wkhtmltopdf/raw/master/wkhtmltox-0.12.3_linux-generic-amd64.tar.xz

	tar -xf /tmp/wkhtmltox.tar.xz -C /tmp
	sudo install -m 0755 \
		/tmp/wkhtmltox/bin/wkhtmltopdf \
		/usr/local/bin/wkhtmltopdf
}

install_wkhtmltopdf &
wkhtmltopdf_pid=$!

# Stop the background download if installation exits early.
cleanup() {
	if kill -0 "$wkhtmltopdf_pid" 2>/dev/null; then
		kill "$wkhtmltopdf_pid" 2>/dev/null || true
		wait "$wkhtmltopdf_pid" 2>/dev/null || true
	fi
}
trap cleanup EXIT

cd ~/frappe-bench || exit

sed -i 's/watch:/# watch:/g' Procfile
sed -i 's/schedule:/# schedule:/g' Procfile
sed -i 's/socketio:/# socketio:/g' Procfile
sed -i 's/redis_socketio:/# redis_socketio:/g' Procfile

echo "::group::Install required apps"
bench get-app https://github.com/frappe/erpnext --branch version-16
bench get-app https://github.com/frappe/hrms --branch version-16
bench get-app https://github.com/frappe/education --branch version-16
bench get-app https://github.com/resilient-tech/india-compliance --branch version-16
bench get-app https://github.com/shridarpatil/frappe_whatsapp --branch master
bench get-app campus "${GITHUB_WORKSPACE}"
bench setup requirements --dev
echo "::endgroup::"

echo "::group::Finish wkhtmltopdf installation"
wait "$wkhtmltopdf_pid"
trap - EXIT
echo "::endgroup::"

echo "::group::Initialize test site"
bench start &>> ~/frappe-bench/bench_start.log &
bench --site test_site reinstall --yes

for app in erpnext hrms education india_compliance frappe_whatsapp campus; do
	bench --verbose --site test_site install-app "$app"
done
echo "::endgroup::"
