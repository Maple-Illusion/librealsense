# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2021 Intel Corporation. All Rights Reserved.

from rspy import log
import sys, os, re



# We need both pyrealsense2 and acroname. We can work without acroname, but
# without rs no devices at all will be returned.
try:
    import pyrealsense2 as rs
    log.d( rs )
    #
    # Have to add site-packages, just in case: if -S was used, or parent script played with
    # sys.path (as run-unit-tests does), then we may not have it!
    sys.path += [os.path.join( os.path.dirname( sys.executable ), 'lib', 'site-packages')]
    #
    try:
        from rspy import acroname
    except ModuleNotFoundError:
        # Error should have already been printed
        # We assume there's no brainstem library, meaning no acroname either
        log.d( 'sys.path=', sys.path )
        acroname = None
    #
    sys.path = sys.path[:-1]  # remove what we added
except ModuleNotFoundError:
    log.w( 'No pyrealsense2 library is available! Running as if no cameras available...' )
    import sys
    log.d( 'sys.path=', sys.path )
    rs = None
    acroname = None

import time

_device_by_sn = dict()
_context = None


class Device:
    def __init__( self, sn, dev ):
        self._sn = sn
        self._dev = dev
        self._name = None
        if dev.supports( rs.camera_info.name ):
            self._name = dev.get_info( rs.camera_info.name )
        self._product_line = None
        if dev.supports( rs.camera_info.product_line ):
            self._product_line = dev.get_info( rs.camera_info.product_line )
        self._physical_port = dev.supports( rs.camera_info.physical_port ) and dev.get_info( rs.camera_info.physical_port ) or None
        self._usb_location = _get_usb_location( self._physical_port )
        self._port = None
        if acroname:
            try:
                self._port = _get_port_by_loc( self._usb_location )
            except Exception as e:
                log.e( 'Failed to get device port:', e )
                log.d( '    physical port is', self._physical_port )
                log.d( '    USB location is', self._usb_location )
        self._removed = False

    @property
    def serial_number( self ):
        return self._sn

    @property
    def name( self ):
        return self._name

    @property
    def product_line( self ):
        return self._product_line

    @property
    def physical_port( self ):
        return self._physical_port

    @property
    def usb_location( self ):
        return self._usb_location

    @property
    def port( self ):
        return self._port

    @property
    def handle( self ):
        return self._dev

    @property
    def enabled( self ):
        return self._removed is False


def query( monitor_changes = True ):
    """
    Start a new LRS context, and collect all devices
    :param monitor_changes: If True, devices will update dynamically as they are removed/added
    """
    global rs
    if not rs:
        return
    #
    # Before we can start a context and query devices, we need to enable all the ports
    # on the acroname, if any:
    if acroname:
        acroname.connect()  # MAY THROW!
        acroname.enable_ports( sleep_on_change = 5 )  # make sure all connected!
    #
    # Get all devices, and store by serial-number
    global _device_by_sn, _context, _port_to_sn
    _context = rs.context()
    _device_by_sn = dict()
    try:
        log.d( 'discovering devices ...' )
        log.debug_indent()
        for dev in _context.query_devices():
            if dev.is_update_device():
                sn = dev.get_info( rs.camera_info.firmware_update_id )
            else:
                sn = dev.get_info( rs.camera_info.serial_number )
            device = Device( sn, dev )
            _device_by_sn[sn] = device
            log.d( '... port {}:'.format( device.port is None and '?' or device.port ), dev )
    finally:
        log.debug_unindent()
    #
    if monitor_changes:
        _context.set_devices_changed_callback( _device_change_callback )


def _device_change_callback( info ):
    """
    Called when librealsense detects a device change (see query())
    """
    global _device_by_sn
    for device in _device_by_sn.values():
        if device.enabled  and  info.was_removed( device.handle ):
            log.d( 'device removed:', device.serial_number )
            device._removed = True
    for handle in info.get_new_devices():
        if handle.is_update_device():
            sn = handle.get_info( rs.camera_info.firmware_update_id )
        else:
            sn = handle.get_info( rs.camera_info.serial_number )
        log.d( 'device added:', handle )
        if sn in _device_by_sn:
            _device_by_sn[sn]._removed = False
        else:
            log.d( 'New device detected!?' )   # shouldn't see new devices...
            _device_by_sn[sn] = Device( sn, handle )


def all():
    """
    :return: A set of all device serial-numbers at the time of query()
    """
    global _device_by_sn
    return _device_by_sn.keys()


def enabled():
    """
    :return: A set of all device serial-numbers that are currently enabled
    """
    global _device_by_sn
    return { device.serial_number for device in _device_by_sn.values() if device.enabled }


def by_product_line( product_line ):
    """
    :param product_line: The product line we're interested in, as a string ("L500", etc.)
    :return: A set of device serial-numbers
    """
    global _device_by_sn
    return { device.serial_number for device in _device_by_sn.values() if device.product_line == product_line }


def by_name( name ):
    """
    :param name: Part of the product name to search for ("L515" would match "Intel RealSense L515")
    :return: A set of device serial-numbers
    """
    global _device_by_sn
    return { device.serial_number for device in _device_by_sn.values() if device.name  and  device.name.find( name ) >= 0 }


def _get_sns_from_spec( spec ):
    """
    Helper function for by_configuration. Yields all serial-numbers matching the given spec
    """
    if spec.endswith( '*' ):
        for sn in by_product_line( spec[:-1] ):
            yield sn
    else:
        for sn in by_name( spec ):
            yield sn


def by_configuration( config ):
    """
    Yields the serial numbers fitting the given configuration. If configuration includes an 'each' directive
    will yield all fitting serial numbers one at a time. Otherwise yields one set of serial numbers fitting the configuration

    :param config: A test:device line collection of arguments (e.g., [L515 D400*])

    If no device matches the configuration devices specified, a RuntimeError will be
    raised!
    """
    if len( config ) == 1 and re.fullmatch( r'each\(.+\)', config[0], re.IGNORECASE ):
        spec = config[0][5:-1]
        for sn in _get_sns_from_spec( spec ):
            yield { sn }
    else:
        sns = set()
        for spec in config:
            old_len = len(sns)
            for sn in _get_sns_from_spec( spec ):
                if sn not in sns:
                    sns.add( sn )
                    break
            new_len = len(sns)
            if new_len == old_len:
                if old_len:
                    raise RuntimeError( 'no device matches configuration "' + spec + '" (after already matching ' + str(sns) + ')' )
                else:
                    raise RuntimeError( 'no device matches configuration "' + spec + '"' )
        if sns:
            yield sns


def get( sn ):
    """
    NOTE: will raise an exception if the SN is unknown!

    :param sn: The serial-number of the requested device
    :return: The pyrealsense2.device object with the given SN, or None
    """
    global _device_by_sn
    return _device_by_sn.get(sn).handle


def get_port( sn ):
    """
    NOTE: will raise an exception if the SN is unknown!

    :param sn: The serial-number of the requested device
    :return: The port number (0-7) the device is on, or None if Acroname interface is unavailable
    """
    global _device_by_sn
    return _device_by_sn.get(sn).port


def enable_only( serial_numbers, recycle = False, timeout = 5 ):
    """
    Enable only the devices corresponding to the given serial-numbers. This can work either
    with or without Acroname: without, the devices will simply be HW-reset, but other devices
    will still be present.

    NOTE: will raise an exception if any SN is unknown!

    :param serial_numbers: A collection of serial-numbers to enable - all others' ports are
                           disabled and will no longer be usable!
    :param recycle: If False, the devices will not be reset if they were already enabled. If
                    True, the devices will be recycled by disabling the port, waiting, then
                    re-enabling
    :param timeout: The maximum seconds to wait to make sure the devices are indeed online
    """
    if acroname:
        #
        ports = [ get_port( sn ) for sn in serial_numbers ]
        #
        if recycle:
            #
            log.d( 'recycling ports via acroname:', ports )
            #
            acroname.disable_ports( acroname.ports() )
            _wait_until_removed( serial_numbers, timeout = timeout )
            #
            acroname.enable_ports( ports )
            #
        else:
            #
            acroname.enable_ports( ports, disable_other_ports = True )
        #
        _wait_for( serial_numbers, timeout = timeout )
        #
    elif recycle:
        #
        hw_reset( serial_numbers )
        #
    else:
        log.d( 'no acroname; ports left as-is' )


def enable_all():
    """
    Enables all ports on an Acroname -- without an Acroname, this does nothing!
    """
    if acroname:
        acroname.enable_ports()


def _wait_until_removed( serial_numbers, timeout = 5 ):
    """
    Wait until the given serial numbers are all offline

    :param serial_numbers: A collection of serial-numbers to wait until removed
    :param timeout: Number of seconds of maximum wait time
    :return: True if all have come offline; False if timeout was reached
    """
    while True:
        have_devices = False
        enabled_sns = enabled()
        for sn in serial_numbers:
            if sn in enabled_sns:
                have_devices = True
                break
        if not have_devices:
            return True
        #
        if timeout <= 0:
            return False
        timeout -= 1
        time.sleep( 1 )


def _wait_for( serial_numbers, timeout = 5 ):
    """
    Wait until the given serial numbers are all online

    :param serial_numbers: A collection of serial-numbers to wait for
    :param timeout: Number of seconds of maximum wait time
    :return: True if all have come online; False if timeout was reached
    """
    did_some_waiting = False
    while True:
        #
        have_all_devices = True
        enabled_sns = enabled()
        for sn in serial_numbers:
            if sn not in enabled_sns:
                have_all_devices = False
                break
        #
        if have_all_devices:
            if did_some_waiting:
                # Wait an extra second, just in case -- let the devices properly power up
                #log.d( 'all devices powered up' )
                time.sleep( 1 )
            return True
        #
        if timeout <= 0:
            if did_some_waiting:
                log.d( 'timed out' )
            return False
        timeout -= 1
        time.sleep( 1 )
        did_some_waiting = True


def hw_reset( serial_numbers, timeout = 5 ):
    """
    Recycles the given devices manually, using a hardware-reset (rather than any acroname port
    reset). The devices are sent a HW-reset command and then we'll wait until they come back
    online.

    NOTE: will raise an exception if any SN is unknown!

    :param serial_numbers: A collection of serial-numbers to reset
    :param timeout: Maximum # of seconds to wait for the devices to come back online
    :return: True if all devices have come back online before timeout
    """
    for sn in serial_numbers:
        dev = get( sn )
        dev.hardware_reset()
    #
    _wait_until_removed( serial_numbers )
    #
    return _wait_for( serial_numbers, timeout = timeout )


###############################################################################################
import platform
if 'windows' in platform.system().lower():
    #
    def _get_usb_location( physical_port ):
        """
        Helper method to get windows USB location from registry
        """
        if not physical_port:
            return None
        # physical port example:
        #   \\?\usb#vid_8086&pid_0b07&mi_00#6&8bfcab3&0&0000#{e5323777-f976-4f5b-9b55-b94699c46e44}\global
        #
        re_result = re.match( r'.*\\(.*)#vid_(.*)&pid_(.*)(?:&mi_(.*))?#(.*)#', physical_port, flags = re.IGNORECASE )
        dev_type = re_result.group(1)
        vid = re_result.group(2)
        pid = re_result.group(3)
        mi = re_result.group(4)
        unique_identifier = re_result.group(5)
        #
        import winreg
        if mi:
            registry_path = "SYSTEM\CurrentControlSet\Enum\{}\VID_{}&PID_{}&MI_{}\{}".format(
                dev_type, vid, pid, mi, unique_identifier
                )
        else:
            registry_path = "SYSTEM\CurrentControlSet\Enum\{}\VID_{}&PID_{}\{}".format(
                dev_type, vid, pid, unique_identifier
                )
        try:
            reg_key = winreg.OpenKey( winreg.HKEY_LOCAL_MACHINE, registry_path )
        except FileNotFoundError:
            log.e( 'Could not find registry key for port:', registry_path )
            log.e( '    usb location:', physical_port )
            return None
        result = winreg.QueryValueEx( reg_key, "LocationInformation" )
        # location example: 0000.0014.0000.016.003.004.003.000.000
        # and, for T265: Port_#0002.Hub_#0006
        return result[0]
    #
    def _get_port_by_loc( usb_location ):
        """
        """
        if usb_location:
            #
            # T265 locations look differently...
            match = re.fullmatch( r'Port_#(\d+)\.Hub_#(\d+)', usb_location, re.IGNORECASE )
            if match:
                # We don't know how to get the port from these yet!
                return None #int(match.group(2))
            else:
                split_location = [int(x) for x in usb_location.split('.') if int(x) > 0]
                # only the last two digits are necessary
                return acroname.get_port_from_usb( split_location[-2], split_location[-1] )
    #
else:
    #
    def _get_usb_location( physical_port ):
        """
        """
        if not physical_port:
            return None
        # physical port example:
        # u'/sys/devices/pci0000:00/0000:00:14.0/usb2/2-3/2-3.3/2-3.3.1/2-3.3.1:1.0/video4linux/video0'
        #
        split_location = physical_port.split( '/' )
        port_location = split_location[-4]
        # location example: 2-3.3.1
        return port_location
    #
    def _get_port_by_loc( usb_location ):
        """
        """
        if usb_location:
            split_port_location = usb_location.split( '.' )
            first_port_coordinate = int( split_port_location[-2] )
            second_port_coordinate = int( split_port_location[-1] )
            port = acroname.get_port_from_usb( first_port_coordinate, second_port_coordinate )
            return port
