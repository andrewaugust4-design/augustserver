server {
    server_name augustserver.com www.augustserver.com;

    root /var/www/augustserver;
    index index.html;

    location / {
        try_files $uri $uri/ =404;
    }



    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/augustserver.com/fullchain.pem; # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/augustserver.com/privkey.pem; # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem; # managed by Certbot

    location /assets/ {
alias /var/www/augustserver/assets/;
    try_files $uri =404;
}
location = /musicreview {
    return 301 /musicreview/;
}

location /musicreview/ {
    proxy_pass http://127.0.0.1:8060/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 100M;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
}# PrivateBin: block direct access to internals (must be ABOVE the php block)
location ~ ^/paste/(data|cfg|lib|tpl|tst|vendor|bin|\.git) {
    deny all;
}

# PrivateBin: run PHP
location ~ ^/paste/.*\.php$ {
    include snippets/fastcgi-php.conf;
    fastcgi_pass unix:/run/php/php8.3-fpm.sock;   # <-- match your ls output from Step 1
}

# PrivateBin: serve index.php at /paste/
location /paste/ {
    index index.php;
    try_files $uri $uri/ /paste/index.php;
}
location = /imagetools {
    return 301 /imagetools/;
}

location /imagetools/ {
    proxy_pass http://127.0.0.1:8061/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 25M;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
location = /drop {
    return 301 /drop/;
}

location /drop/ {
    proxy_pass http://127.0.0.1:8062/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 500M;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
    proxy_request_buffering off;
}
location = /convert {
    return 301 /convert/;
}

location /convert/ {
    proxy_pass http://127.0.0.1:8063/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 500M;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
    proxy_request_buffering off;
}
location = /video {
    return 301 /video/;
}

location /video/ {
    proxy_pass http://127.0.0.1:8064/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 900s;
    proxy_send_timeout 900s;
    proxy_buffering off;
}

}

server {
    if ($host = www.augustserver.com) {
        return 301 https://$host$request_uri;
    } # managed by Certbot


    if ($host = augustserver.com) {
        return 301 https://$host$request_uri;
    } # managed by Certbot


    listen 80 default_server;
    server_name augustserver.com www.augustserver.com;
    return 404; # managed by Certbot

    location /assets/ {
    alias /var/www/shared/;
    try_files $uri =404;
}



}
