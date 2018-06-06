from stem.descriptor.router_status_entry import RouterStatusEntryV3

import sbws.util.stem as stem_utils
from stem import Flag
from stem import DescriptorUnavailable
from stem.util.connection import is_valid_ipv4_address
from stem.util.connection import is_valid_ipv6_address
import random
import time
import logging
from sbws.globals import resolve

log = logging.getLogger(__name__)


class RelayNS(RouterStatusEntryV3):
    """Inherit from RouterStatusEntryV3 and add the attribute
    master_key_ed25519.

    :param str ed25519: the ed25519 master key base 64 encoded.
    """
    def __init__(self, ed25519=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if ed25519 is not None:
            self.master_key_ed25519 = ed25519


class RelayList:
    ''' Keeps a list of all relays in the current Tor network and updates it
    transparently in the background. Provides useful interfaces for getting
    only relays of a certain type.
    '''
    REFRESH_INTERVAL = 300  # seconds

    def __init__(self, args, conf, controller):
        self._controller = controller
        self.rng = random.SystemRandom()
        self._refresh()

    @property
    def relays(self):
        if time.time() >= self._last_refresh + self.REFRESH_INTERVAL:
            self._refresh()
        return self._relays

    @property
    def fast(self):
        return self._relays_with_flag(Flag.FAST)

    @property
    def slow(self):
        ''' Returns relays without the Fast flag '''
        return self._relays_without_flag(Flag.FAST)

    @property
    def exits(self):
        return self._relays_with_flag(Flag.EXIT)

    @property
    def guards(self):
        return self._relays_with_flag(Flag.GUARD)

    @property
    def hsdirs(self):
        return self._relays_with_flag(Flag.HSDIR)

    @property
    def authorities(self):
        return self._relays_with_flag(Flag.AUTHORITY)

    @property
    def unmeasured(self):
        ''' SEEMS BROKEN in stem 1.6.0 as it always returns no relays '''
        relays = self.relays
        # return [r for r in relays if r.measured is None]
        return [r for r in relays if r.is_unmeasured]

    @property
    def measured(self):
        ''' SEEMS BROKEN in stem 1.6.0 as it always returns all relays '''
        relays = self.relays
        # return [r for r in relays if r.measured is not None]
        return [r for r in relays if not r.is_unmeasured]

    def relay_ed25519_master_key(self, ns):
        """Obtain ed25519 master key of the relay represented by
        the network status relay line.

        :param RouterStatusEntryV3 ns: the network status relay
        :returns: str, the ed25519 master key base 64 encoded without
            trailing '='s.
        """
        # In theory this is never going to be the case?
        if ns.identifier is None or ns.identifier_type != 'ed25519':
            log.debug('Getting microdescriptor to obtain ed25519 identity.')
            mdesc = self._controller.get_microdescriptor(ns.fingerprint, None)
            if mdesc is not None:
                if 'ed25519' in mdesc.identifiers.keys():
                    ed25519 = mdesc.identifiers['ed25519'].rstrip('=')
                    log.debug('Found ed25519 master key.')
                    return ed25519
                log.debug('No ed25519 master-key found')
            log.debug('Could not get microdescriptor')
            # In case Tor can not retrive microdescriptors,
            # try with server descriptors.
            log.debug('Getting server descriptor to obtain '
                      'ed25519 master key.')
            sdesc = self._controller.get_server_descriptor(ns.fingerprint,
                                                           None)
            if sdesc is not None:
                ed25519 = sdesc.ed25519_master_key().rstrip('=')
                log.debug('Found ed25519 master key.')
                return ed25519
            log.debug('Could not get server descriptor')
            return None
        log.debug('Relay has already ed25519 master key')
        return ns.identifier

    def exits_can_exit_to(self, host, port):
        '''
        Return exits that can MOST LIKELY exit to the given host:port. **host**
        can be a hostname, but be warned that we will resolve it locally and
        use the first (arbitrary/unknown order) result when checking exit
        policies, which is different than what other parts of the code may do
        (leaving it up to the exit to resolve the name).

        An exit can only MOST LIKELY not just because of the above DNS
        disconnect, but also because fundamentally our Tor client is most
        likely using microdescriptors which do not have full information about
        exit policies.
        '''
        c = self._controller
        if not is_valid_ipv4_address(host) and not is_valid_ipv6_address(host):
            # It certainly isn't perfect trying to guess if an exit can connect
            # to an ipv4/6 address based on the DNS result we got locally. But
            # it's the best we can do.
            #
            # Also, only use the first ipv4/6 we get even if there is more than
            # one.
            host = resolve(host)[0]
        assert is_valid_ipv4_address(host) or is_valid_ipv6_address(host)
        exits = []
        for exit in self.exits:
            # If we have the exit policy already, easy
            if exit.exit_policy:
                policy = exit.exit_policy
            else:
                # Otherwise ask Tor for the microdescriptor and assume the exit
                # won't work if the desc isn't available
                try:
                    fp = exit.fingerprint
                    policy = c.get_microdescriptor(fp).exit_policy
                except DescriptorUnavailable as e:
                    log.debug(e)
                    continue
            # There's a weird KeyError we sometimes hit when checking
            # policy.can_exit_to()... so catch that and log about it. Maybe
            # someday it can be fixed?
            try:
                if policy is not None and policy.can_exit_to(port=port):
                    exits.append(exit)
            except KeyError as e:
                log.exception('Got that KeyError in stem again...: %s', e)
                continue
        return exits

    def random_relay(self):
        relays = self.relays
        return self.rng.choice(relays)

    def _relays_with_flag(self, flag):
        relays = self.relays
        return [r for r in relays if flag in r.flags]

    def _relays_without_flag(self, flag):
        relays = self.relays
        return [r for r in relays if flag not in r.flags]

    def _init_relays(self):
        c = self._controller
        assert stem_utils.is_controller_okay(c)
        relays = []
        # for each network status relay, obtain the ed25519 master key
        # and generate a new list of RelayNS objects
        for ns in c.get_network_statuses():
            ed25519 = self.relay_ed25519_master_key(ns)
            rns = RelayNS(ed25519=ed25519, content=ns._raw_contents)
            relays.append(rns)
        return relays

    def _refresh(self):
        self._relays = self._init_relays()
        self._last_refresh = time.time()
