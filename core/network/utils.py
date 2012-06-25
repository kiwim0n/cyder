from core.network.models import Network

def calc_networks(network):
    network.update_network()
    eldars = []
    sub_networks = []
    for pnet in Network.objects.all():
        pnet.update_network()
        if pnet.pk == network.pk:
            continue
        if pnet.network.overlaps(network.network):
            if pnet.prefixlen > network.prefixlen:
                sub_networks.append(pnet)
            else:
                eldars.append(pnet)
    return eldars, sub_networks

def calc_parent(network):
    eldars, sub_net = calc_networks(network)
    if not eldars:
        return None
    parent = list(reversed(sorted(eldars, key=lambda n: n.prefixlen)))[0]
    return parent
