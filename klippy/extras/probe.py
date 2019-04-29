# Z-Probe support
#
# Copyright (C) 2017-2019  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import pins, homing, manual_probe

HINT_TIMEOUT = """
Make sure to home the printer before probing. If the probe
did not move far enough to trigger, then consider reducing
the Z axis minimum position so the probe can travel further
(the Z minimum position can be negative).
"""

class PrinterProbe:
    def __init__(self, config, mcu_probe):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.mcu_probe = mcu_probe
        self.speed = config.getfloat('speed', 5.0)
        self.x_offset = config.getfloat('x_offset', 0.)
        self.y_offset = config.getfloat('y_offset', 0.)
        self.z_offset = config.getfloat('z_offset')
        self.probe_calibrate_z = 0.
        # Infer Z position to move to during a probe
        if config.has_section('stepper_z'):
            zconfig = config.getsection('stepper_z')
            self.z_position = zconfig.getfloat('position_min', 0.)
        else:
            pconfig = config.getsection('printer')
            self.z_position = pconfig.getfloat('minimum_z_position', 0.)
        # Register z_virtual_endstop pin
        self.printer.lookup_object('pins').register_chip('probe', self)
        # Register PROBE/QUERY_PROBE commands
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('PROBE', self.cmd_PROBE,
                                    desc=self.cmd_PROBE_help)
        self.gcode.register_command('QUERY_PROBE', self.cmd_QUERY_PROBE,
                                    desc=self.cmd_QUERY_PROBE_help)
        self.gcode.register_command('PROBE_CALIBRATE', self.cmd_PROBE_CALIBRATE,
                                    desc=self.cmd_PROBE_CALIBRATE_help)
        self.gcode.register_command('PROBE_ACCURACY', self.cmd_PROBE_ACCURACY,
                                    desc=self.cmd_PROBE_ACCURACY_help)
    def setup_pin(self, pin_type, pin_params):
        if pin_type != 'endstop' or pin_params['pin'] != 'z_virtual_endstop':
            raise pins.error("Probe virtual endstop only useful as endstop pin")
        if pin_params['invert'] or pin_params['pullup']:
            raise pins.error("Can not pullup/invert probe virtual endstop")
        return self.mcu_probe
    def get_offsets(self):
        return self.x_offset, self.y_offset, self.z_offset
    cmd_PROBE_help = "Probe Z-height at current XY position"
    def cmd_PROBE(self, params):
        self._probe(self.speed)
    def _probe(self, speed):
        toolhead = self.printer.lookup_object('toolhead')
        homing_state = homing.Homing(self.printer)
        pos = toolhead.get_position()
        pos[2] = self.z_position
        endstops = [(self.mcu_probe, "probe")]
        verify = self.printer.get_start_args().get('debugoutput') is None
        try:
            homing_state.homing_move(pos, endstops, speed,
                                     probe_pos=True, verify_movement=verify)
        except homing.EndstopError as e:
            reason = str(e)
            if "Timeout during endstop homing" in reason:
                reason += HINT_TIMEOUT
            raise self.gcode.error(reason)
        pos = toolhead.get_position()
        self.gcode.respond_info("probe at %.3f,%.3f is z=%.6f" % (
            pos[0], pos[1], pos[2]))
        self.gcode.reset_last_position()
    cmd_QUERY_PROBE_help = "Return the status of the z-probe"
    def cmd_QUERY_PROBE(self, params):
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        self.mcu_probe.query_endstop(print_time)
        res = self.mcu_probe.query_endstop_wait()
        self.gcode.respond_info(
            "probe: %s" % (["open", "TRIGGERED"][not not res],))
    cmd_PROBE_ACCURACY_help = "Probe Z-height accuracy at current XY position"
    def cmd_PROBE_ACCURACY(self, params):
        toolhead = self.printer.lookup_object('toolhead')
        probes = []
        pos = toolhead.get_position()
        number_of_reads = self.gcode.get_int('REPEAT', params, default=10,
                                                       minval=4, maxval=50)
        speed = self.gcode.get_int('SPEED', params, default=self.speed,
                                            minval=1, maxval=30)
        z_start_position = self.gcode.get_float(
            'Z', params, default=10., minval=self.z_offset, maxval=70.)
        x_start_position = self.gcode.get_float('X', params, default=pos[0])
        y_start_position = self.gcode.get_float('Y', params, default=pos[1])
        start_pos = [x_start_position, y_start_position, z_start_position]
        self.gcode.respond_info("probe accuracy: at X:%.3f Y:%.3f Z:%.3f\n"
                                "                "
                                "and read %d times with speed of %d mm/s" % (
                                x_start_position, y_start_position,
                                z_start_position, number_of_reads, speed))
        # Probe bed "number_of_reads" times
        sum_reads = 0
        for i in range(number_of_reads):
            # Move Z to start reading position
            self._move(start_pos, speed)
            # Probe
            self._probe(speed)
            # Get Z value, accumulate value to calculate average
            # and save it to calculate standard deviation
            pos = toolhead.get_position()
            sum_reads += pos[2]
            probes.append(pos[2])
        # Move Z to start reading position
        self._move(start_pos, speed)
        # Calculate maximum, minimum and average values
        max_value = max(probes)
        min_value = min(probes)
        avg_value = sum(probes) / number_of_reads
        # calculate the standard deviation
        deviation_sum = 0
        for i in range(number_of_reads):
            deviation_sum += pow(probes[i] - avg_value, 2)
        sigma = (deviation_sum / number_of_reads) ** 0.5
        # Median
        sorted_probes = sorted(probes)
        middle = number_of_reads//2
        if (number_of_reads & 1) == 1:
            # odd number of reads
            median = sorted_probes[middle]
        else:
            # even number of reads
            median = (sorted_probes[middle]+sorted_probes[middle-1])/2
        # Show information
        self.gcode.respond_info(
            "probe accuracy results: maximum %.6f, minimum %.6f, "
            "average %.6f, median %.6f, standard deviation %.6f" % (
            max_value, min_value, avg_value, median, sigma))
    def _move(self, coord, speed):
        toolhead = self.printer.lookup_object('toolhead')
        curpos = toolhead.get_position()
        for i in range(len(coord)):
            if coord[i] is not None:
                curpos[i] = coord[i]
        try:
            toolhead.move(curpos, speed)
        except homing.EndstopError as e:
            raise self.gcode.error(str(e))
    def probe_calibrate_finalize(self, kin_pos):
        if kin_pos is None:
            return
        z_offset = self.probe_calibrate_z - kin_pos[2]
        self.gcode.respond_info(
            "%s: z_offset: %.3f\n"
            "The SAVE_CONFIG command will update the printer config file\n"
            "with the above and restart the printer." % (self.name, z_offset))
        configfile = self.printer.lookup_object('configfile')
        configfile.set(self.name, 'z_offset', "%.3f" % (z_offset,))
    cmd_PROBE_CALIBRATE_help = "Calibrate the probe's z_offset"
    def cmd_PROBE_CALIBRATE(self, params):
        # Perform initial probe
        self._probe(self.speed)
        # Move away from the bed
        toolhead = self.printer.lookup_object('toolhead')
        curpos = toolhead.get_position()
        self.probe_calibrate_z = curpos[2]
        curpos[2] += 5.
        self._move(curpos, self.speed)
        # Move the nozzle over the probe point
        curpos[0] += self.x_offset
        curpos[1] += self.y_offset
        self._move(curpos, self.speed)
        # Start manual probe
        manual_probe.ManualProbeHelper(self.printer, params,
                                       self.probe_calibrate_finalize)

# Endstop wrapper that enables probe specific features
class ProbeEndstopWrapper:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.position_endstop = config.getfloat('z_offset')
        gcode_macro = self.printer.try_load_module(config, 'gcode_macro')
        self.activate_gcode = gcode_macro.load_template(
            config, 'activate_gcode')
        self.deactivate_gcode = gcode_macro.load_template(
            config, 'deactivate_gcode')
        # Create an "endstop" object to handle the probe pin
        ppins = self.printer.lookup_object('pins')
        pin = config.get('pin')
        pin_params = ppins.lookup_pin(pin, can_invert=True, can_pullup=True)
        mcu = pin_params['chip']
        mcu.register_config_callback(self._build_config)
        self.mcu_endstop = mcu.setup_pin('endstop', pin_params)
        # Wrappers
        self.get_mcu = self.mcu_endstop.get_mcu
        self.add_stepper = self.mcu_endstop.add_stepper
        self.get_steppers = self.mcu_endstop.get_steppers
        self.home_start = self.mcu_endstop.home_start
        self.home_wait = self.mcu_endstop.home_wait
        self.query_endstop = self.mcu_endstop.query_endstop
        self.query_endstop_wait = self.mcu_endstop.query_endstop_wait
        self.TimeoutError = self.mcu_endstop.TimeoutError
    def _build_config(self):
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        for stepper in kin.get_steppers('Z'):
            stepper.add_to_endstop(self)
    def home_prepare(self):
        try:
            self.activate_gcode.run_gcode_from_command()
        except self.gcode.error as e:
            raise homing.EndstopError(str(e))
        self.mcu_endstop.home_prepare()
    def home_finalize(self):
        try:
            self.deactivate_gcode.run_gcode_from_command()
        except self.gcode.error as e:
            raise homing.EndstopError(str(e))
        self.mcu_endstop.home_finalize()
    def get_position_endstop(self):
        return self.position_endstop

# Helper code that can probe a series of points and report the
# position at each point.
class ProbePointsHelper:
    def __init__(self, config, finalize_callback, default_points=None):
        self.printer = config.get_printer()
        self.finalize_callback = finalize_callback
        self.probe_points = default_points
        # Read config settings
        if default_points is None or config.get('points', None) is not None:
            points = config.get('points').split('\n')
            try:
                points = [line.split(',', 1) for line in points if line.strip()]
                self.probe_points = [(float(p[0].strip()), float(p[1].strip()))
                                     for p in points]
            except:
                raise config.error("Unable to parse probe points in %s" % (
                    config.get_name()))
        if len(self.probe_points) < 3:
            raise config.error("Need at least 3 probe points for %s" % (
                config.get_name()))
        self.horizontal_move_z = config.getfloat('horizontal_move_z', 5.)
        self.speed = self.lift_speed = config.getfloat('speed', 50., above=0.)
        self.probe_offsets = (0., 0., 0.)
        self.samples = config.getint('samples', 1, minval=1)
        self.sample_retract_dist = config.getfloat(
            'sample_retract_dist', 2., above=0.)
        self.samples_result = config.getchoice('samples_result',
                                               {'median': 0, 'average': 1},
                                               default='average')
        # Internal probing state
        self.results = []
        self.busy = self.manual_probe = False
        self.gcode = self.toolhead = None
    def get_lift_speed(self):
        return self.lift_speed
    def _lift_z(self, z_pos, add=False, speed=None):
        # Lift toolhead
        curpos = self.toolhead.get_position()
        if add:
            curpos[2] += z_pos
        else:
            curpos[2] = z_pos
        if speed is None:
            speed = self.lift_speed
        try:
            self.toolhead.move(curpos, speed)
        except homing.EndstopError as e:
            self._finalize(False)
            raise self.gcode.error(str(e))
    def _move_next(self):
        # Lift toolhead
        self._lift_z(self.horizontal_move_z)
        # Check if done probing
        if len(self.results) >= len(self.probe_points):
            self.toolhead.get_last_move_time()
            self._finalize(True)
            return
        # Move to next XY probe point
        x, y = self.probe_points[len(self.results)]
        curpos = self.toolhead.get_position()
        curpos[0] = x
        curpos[1] = y
        curpos[2] = self.horizontal_move_z
        try:
            self.toolhead.move(curpos, self.speed)
        except homing.EndstopError as e:
            self._finalize(False)
            raise self.gcode.error(str(e))
        self.gcode.reset_last_position()
        if self.manual_probe:
            manual_probe.ManualProbeHelper(self.printer, {},
                                           self._manual_probe_finalize)
    def _automatic_probe_point(self):
        positions = []
        for i in range(self.samples):
            try:
                self.gcode.run_script_from_command("PROBE")
            except self.gcode.error as e:
                self._finalize(False)
                raise
            positions.append(self.toolhead.get_position())
            if i < self.samples - 1:
                # retract
                self._lift_z(self.sample_retract_dist, add=True)
        if self.samples_result == 1:
            # Calculate Average
            calculated_value = [sum([pos[i] for pos in positions]) /
                                self.samples for i in range(3)]
        else:
            # Calculate Median
            sorted_z_positions = sorted([position[2]
                                         for position in positions])
            middle = self.samples // 2
            if (self.samples & 1) == 1:
                # odd number of samples
                median = sorted_z_positions[middle]
            else:
                # even number of samples
                median = (sorted_z_positions[middle] +
                          sorted_z_positions[middle - 1]) / 2
            calculated_value = [positions[0][0],
                                positions[0][1],
                                median]
        self.results.append(calculated_value)
    def start_probe(self, params):
        # Lookup objects
        self.toolhead = self.printer.lookup_object('toolhead')
        self.gcode = self.printer.lookup_object('gcode')
        probe = self.printer.lookup_object('probe', None)
        method = self.gcode.get_str('METHOD', params, 'automatic').lower()
        if probe is not None and method == 'automatic':
            self.manual_probe = False
            self.lift_speed = min(self.speed, probe.speed)
            self.probe_offsets = probe.get_offsets()
            if self.horizontal_move_z < self.probe_offsets[2]:
                raise self.gcode.error("horizontal_move_z can't be less than"
                                       " probe's z_offset")
        else:
            self.manual_probe = True
            self.lift_speed = self.speed
            self.probe_offsets = (0., 0., 0.)
        # Start probe
        self.results = []
        self.busy = True
        self._lift_z(self.horizontal_move_z, speed=self.speed)
        self._move_next()
        if not self.manual_probe:
            # Perform automatic probing
            while self.busy:
                self._automatic_probe_point()
                self._move_next()
    def _manual_probe_finalize(self, kin_pos):
        if kin_pos is None:
            self._finalize(False)
            return
        self.results.append(kin_pos)
        self._move_next()
    def _finalize(self, success):
        self.busy = False
        self.gcode.reset_last_position()
        if success:
            self.finalize_callback(self.probe_offsets, self.results)

def load_config(config):
    return PrinterProbe(config, ProbeEndstopWrapper(config))
