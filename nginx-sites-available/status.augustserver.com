server {
    server_name status.augustserver.com;

    root /var/www/status;
    index index.html;

    location / {
        try_files $uri $uri/ =404;
    }

    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/status.augustserver.com/fullchain.pem; # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/status.augustserver.com/privkey.pem; # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem; # managed by Certbot

   location /assets/ {
    alias /var/www/augustserver/assets/;
    try_files $uri =404;
}


}
server {
    if ($host = status.augustserver.com) {
        return 301 https://$host$request_uri;
    } # managed by Certbot


    listen 80;
    server_name status.augustserver.com;
    return 404; # managed by Certbot
    
    location /assets/ {
    alias /var/www/shared/;
    try_files $uri =404;
}



}
