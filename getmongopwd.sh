#!/bin/bash
if [[ $# -eq 0 ]]; then
    echo ""
    echo "Usage: $0 <hostname> [<hostname> ...]"
    echo "       $0 -f <hostfile>"
    echo ""
    exit
fi

if [[ "$1" == "-f" ]]; then
    if [[ -z "$2" || ! -f "$2" ]]; then
        echo "Error: File '$2' not found"
        exit 1
    fi
    hostlist=$(grep -v '^\s*$' "$2")
else
    hostlist="$(echo "$@" | sed -e 's/,/ /g')"
fi

vltkn=$(grep token: /etc/salt/master.d/vault.conf | awk '{print $2}')
vlturl=$(grep url /etc/salt/master.d/vault.conf | awk '{print $2}')

GetDomain(){
    base=$(echo "$host" | sed 's/ctm[0-9]*$//')

    if [[ "${base}" =~ ^(.+)(ch3|lo5|s1|ca|br|de|sg|jpe)$ ]]; then
        custid="${BASH_REMATCH[1]}"
        dc="${BASH_REMATCH[2]}"
        [[ "$dc" == "jpe" ]] && dc="sg"
        if [[ "$dc" =~ ^(ca|br|de|sg)$ ]]; then
            vregion=$(grep region.vault.store /var/tmp/govern-tools/.regiondata/az$dc.sls | awk '{print $2}' | tr -d \')
        else
            vregion=$(grep region.vault.store /var/tmp/govern-tools/.regiondata/$dc.sls | awk '{print $2}' | tr -d \')
        fi
    else
        echo "# WARN: Could not parse domain from host: $host" >&2
        vregion=""
        custid=""
    fi
}

Main(){
    while IFS= read -r host; do
        host=$(echo "$host" | tr -d '[:space:]')
        [[ -z "$host" ]] && continue
        GetDomain
        if [[ -z "$vregion" || -z "$custid" ]]; then
            echo "passwords[\"$host\"]=\"ERROR: could not determine vault path\""
            continue
        fi
        pwd=$(curl -s --header "X-Vault-Token:$vltkn" --request GET "$vlturl"/v1/"$vregion"/"$custid"/tm/mongodbpassPW | jq -r '.data.password')
        echo "passwords[\"$host\"]=\"$pwd\""
    done <<< "$hostlist"
}

Main
