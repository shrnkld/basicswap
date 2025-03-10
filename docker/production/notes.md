# Split container setup

This will setup Basicswap so that each coin runs in it's own container.


Copy and edit .env config:

    cp example.env .env


Set the latest Monero chain height, or the height your wallet must restore from:

    echo "DEFAULT_XMR_RESTORE_HEIGHT=$(curl https://localmonero.co/blocks/api/get_stats | jq .height)" >> .env


Create docker-compose config:

    cat compose-fragments/0_start.yml > docker-compose.yml

    # Add the relevant coin fragments
    cat compose-fragments/1_bitcoin.yml >> docker-compose.yml
    cat compose-fragments/1_litecoin.yml >> docker-compose.yml
    cat compose-fragments/1_monero-wallet.yml >> docker-compose.yml

    # Copy for prepare script config
    cp docker-compose.yml docker-compose-prepare.yml
    cat compose-fragments/9_swapprepare.yml >> docker-compose-prepare.yml

    # Add the Monero daemon if required (should not go in docker-compose-prepare.yml)
    cat compose-fragments/8_monero-daemon.yml >> docker-compose.yml
    # Add the swapclient
    cat compose-fragments/8_swapclient.yml >> docker-compose.yml


Create the docker network, with a specific subnet (for optional tor use):

    docker network create coinswap_network --subnet="172.16.238.0/24"


Build the swapclient container:

    docker-compose build swapclient


Build the monero container, if required:

    docker-compose build monero_daemon


Build the remaining coin containers:

    docker-compose build


Build the prepare-only containers:

    docker-compose -f docker-compose-prepare.yml build


Create config files:

    # Select relevant coins:
    export WITH_COINS=bitcoin,litecoin,monero

    docker-compose -f docker-compose-prepare.yml run --rm swapprepare \
        basicswap-prepare --nocores --withcoins=${WITH_COINS} --htmlhost="0.0.0.0" --particl_mnemonic=none


Start coin cores only:

    docker-compose -f docker-compose-prepare.yml up -d --scale swapprepare=0


Initialise wallets:

    docker-compose -f docker-compose-prepare.yml run --rm swapprepare \
        basicswap-prepare --initwalletsonly


Stop cores:

    docker-compose -f docker-compose-prepare.yml stop


Start BasicSwap:

    docker-compose up


