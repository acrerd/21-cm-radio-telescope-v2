// WT32-ETH01 Enclosure for External Box Mounting
// Designed to mount on outside of larger enclosure with wire feedthrough

// ============================================
// PARAMETERS - Adjust these to fit your needs
// ============================================

// WT32-ETH01 Board Dimensions (measure your board!)
// Note: Board is ~55mm long x 25mm wide, but pin headers add ~1mm each side
board_length = 55;      // Length of PCB (mm)
board_width = 25;       // Width of PCB (mm) - actual PCB width
board_thickness = 1.6;  // PCB thickness (mm)
component_height = 13;  // Height of tallest component (Ethernet jack ~13.5mm)

// Enclosure Parameters
wall_thickness = 2.5;           // Wall thickness
clearance = 1.0;                // Clearance around board
extension_length = 25;          // Extra length for wire routing and screw posts
wire_hole_diameter = 15;        // Diameter of wire feedthrough hole

// Floor mounting holes (countersunk, under the board)
floor_mount_hole_diameter = 4;      // Hole for self-tapping screw shaft
floor_mount_countersink = 8;        // Countersink diameter (for screw head)
floor_mount_countersink_depth = 2;  // Countersink depth

// Lid parameters
lid_lip_height = 3;             // Height of lid overlap lip
lid_tolerance = 0.3;            // Tolerance for lid fit

// Ethernet port cutout (RJ45 connector is narrower than board)
eth_port_width = 16;            // Standard RJ45 width (~16mm)
eth_port_height = 13.5;         // Standard RJ45 height (~13.5mm)
eth_port_clearance = 0.5;       // Extra clearance around connector

// Screw post parameters for lid
screw_post_diameter = 6;
screw_hole_diameter = 2.5;      // For M3 self-tapping screws
screw_post_height = 3;

// Board support rail height
rail_height = 3;

// ============================================
// CALCULATED VALUES
// ============================================

// Internal dimensions
// No extra length at Ethernet end - RJ45 will be flush with end wall
internal_length = board_length + extension_length + clearance * 2;
internal_width = board_width + clearance * 2;
// Height: rails + board + components + clearance above
internal_height = rail_height + board_thickness + component_height + clearance;

// Board position (for reference)
board_start_x = wall_thickness + extension_length + clearance;
board_end_x = board_start_x + board_length;

// External dimensions (box only, not including flanges)
external_length = internal_length + wall_thickness * 2;
external_width = internal_width + wall_thickness * 2;
external_height = internal_height + wall_thickness;  // No top wall (lid goes there)

// ============================================
// MODULES
// ============================================

// Rounded box helper
module rounded_box(length, width, height, radius) {
    hull() {
        for (x = [radius, length - radius]) {
            for (y = [radius, width - radius]) {
                translate([x, y, 0])
                    cylinder(r=radius, h=height, $fn=32);
            }
        }
    }
}

// Main enclosure base
module enclosure_base() {
    corner_radius = 3;

    // Screw post positions - only at back (wire) end, 2 posts total
    post_x = wall_thickness + extension_length - screw_post_diameter/2 - 2;
    post_y_inset = wall_thickness + screw_post_diameter/2 + 0.5;

    union() {
        difference() {
            // Main box body
            rounded_box(external_length, external_width, external_height, corner_radius);

            // Hollow out the interior
            translate([wall_thickness, wall_thickness, wall_thickness])
                rounded_box(internal_length, internal_width, internal_height + 1, corner_radius - 1);

            // Floor mounting holes - countersunk, positioned under the board
            // Two holes spaced along the board length
            floor_mount_x1 = board_start_x + board_length * 0.25;
            floor_mount_x2 = board_start_x + board_length * 0.75;
            floor_mount_y = external_width / 2;

            for (x = [floor_mount_x1, floor_mount_x2]) {
                // Through hole for screw shaft
                translate([x, floor_mount_y, -1])
                    cylinder(d=floor_mount_hole_diameter, h=wall_thickness + 2, $fn=24);
                // Countersink from inside (top of floor)
                translate([x, floor_mount_y, wall_thickness - floor_mount_countersink_depth])
                    cylinder(d1=floor_mount_hole_diameter, d2=floor_mount_countersink,
                             h=floor_mount_countersink_depth + 0.1, $fn=24);
            }

            // Wire feedthrough hole (at extension end, through bottom)
            translate([wall_thickness + extension_length/2, external_width/2, -1])
                cylinder(d=wire_hole_diameter, h=wall_thickness + 2, $fn=48);

            // Ethernet port cutout - flush with end wall
            // The RJ45 jack (~16mm wide) is narrower than the board (~25mm) and centered
            eth_cutout_width = eth_port_width + eth_port_clearance * 2;
            eth_cutout_height = eth_port_height + eth_port_clearance * 2;
            eth_y = (external_width - eth_cutout_width) / 2;  // Centered in enclosure width
            eth_z = wall_thickness + rail_height + board_thickness - eth_port_clearance;  // Board on rails
            // Cut through the end wall - RJ45 flush with outside
            translate([external_length - wall_thickness - 1, eth_y, eth_z])
                cube([wall_thickness + 2, eth_cutout_width, eth_cutout_height]);
        }  // end difference for main box

        // Screw posts - only 2 at the back (wire) end
        for (y = [post_y_inset, external_width - post_y_inset]) {
            difference() {
                translate([post_x, y, wall_thickness])
                    cylinder(d=screw_post_diameter, h=external_height - wall_thickness, $fn=24);
                // Screw hole in post
                translate([post_x, y, external_height - 12])
                    cylinder(d=screw_hole_diameter, h=13, $fn=24);
            }
        }
    }  // end union

    // Board support rails - short sections at each end for pin clearance
    rail_width = 2;
    rail_stub_length = 5;  // Short stubs at each end

    // Front stubs (wire routing end of board)
    translate([board_start_x, wall_thickness + clearance, wall_thickness])
        cube([rail_stub_length, rail_width, rail_height]);
    translate([board_start_x, external_width - wall_thickness - clearance - rail_width, wall_thickness])
        cube([rail_stub_length, rail_width, rail_height]);

    // Rear stubs (Ethernet end of board)
    translate([board_end_x - rail_stub_length, wall_thickness + clearance, wall_thickness])
        cube([rail_stub_length, rail_width, rail_height]);
    translate([board_end_x - rail_stub_length, external_width - wall_thickness - clearance - rail_width, wall_thickness])
        cube([rail_stub_length, rail_width, rail_height]);

    // Lid retention lips at Ethernet end - on either side of the Ethernet cutout
    // These overhang inward for the lid tongue to slip under
    lip_overhang = 2;       // How far the lip extends inward
    lip_thickness = 3;      // Doubled for stronger retention    // Thickness of the lip
    lip_width = 8;          // Width of each lip section
    eth_cutout_width = eth_port_width + eth_port_clearance * 2;
    eth_y_start = (external_width - eth_cutout_width) / 2;
    eth_y_end = eth_y_start + eth_cutout_width;

    // Left lip (low Y side of Ethernet)
    translate([external_length - wall_thickness - lip_overhang,
               wall_thickness,
               external_height - lip_thickness])
        cube([lip_overhang, eth_y_start - wall_thickness - 1, lip_thickness]);

    // Right lip (high Y side of Ethernet)
    translate([external_length - wall_thickness - lip_overhang,
               eth_y_end + 1,
               external_height - lip_thickness])
        cube([lip_overhang, external_width - eth_y_end - wall_thickness - 1, lip_thickness]);
}

// Lid module
module enclosure_lid() {
    corner_radius = 3;
    lid_height = wall_thickness + lid_lip_height;
    lip_ridge_thickness = 1.5;  // Thickness of the lip ridge

    // Outer dimensions match the box
    outer_length = external_length;
    outer_width = external_width;

    // Lip dimensions (fits inside the box)
    lip_length = internal_length - lid_tolerance * 2;
    lip_width = internal_width - lid_tolerance * 2;

    // Screw post position (must match base) - only 2 posts at back
    post_x = wall_thickness + extension_length - screw_post_diameter/2 - 2;
    post_y_inset = wall_thickness + screw_post_diameter/2 + 0.5;

    // Tongue dimensions (must match enclosure lips)
    lip_overhang = 2;
    lip_thickness = 3;      // Doubled for stronger retention
    tongue_thickness = 2.5;  // Thicker for rigidity
    tongue_extension = 6;    // How far tongue extends back into lip ridge
    eth_cutout_width = eth_port_width + eth_port_clearance * 2;
    eth_y_start = (external_width - eth_cutout_width) / 2;
    eth_y_end = eth_y_start + eth_cutout_width;

    // Tongue position: sits below the lip with gap above
    tongue_gap = lip_thickness + 0.3;
    tongue_z = -tongue_gap - tongue_thickness;

    difference() {
        union() {
            // Main lid plate
            rounded_box(outer_length, outer_width, wall_thickness, corner_radius);

            // Inner lip as a ridge (hollow) - saves material
            // Cut short at Ethernet end where tongues begin
            // Tongue transition point - where lip ridge ends and tongue vertical begins
            tongue_transition_x = outer_length - wall_thickness - lip_overhang - lip_ridge_thickness;

            difference() {
                translate([wall_thickness + lid_tolerance, wall_thickness + lid_tolerance, -lid_lip_height])
                    rounded_box(lip_length, lip_width, lid_lip_height, corner_radius - 1);
                // Hollow out the inside, leaving just a ridge
                translate([wall_thickness + lid_tolerance + lip_ridge_thickness,
                          wall_thickness + lid_tolerance + lip_ridge_thickness,
                          -lid_lip_height - 1])
                    rounded_box(lip_length - lip_ridge_thickness * 2,
                               lip_width - lip_ridge_thickness * 2,
                               lid_lip_height + 2,
                               corner_radius - 1);
                // Remove lip ridge at Ethernet end - cut from where tongues start
                translate([tongue_transition_x,
                          -1,
                          -lid_lip_height - 1])
                    cube([outer_length, outer_width + 2, lid_lip_height + 2]);
            }

            // Tongues at Ethernet end - L-shape connects to lip ridge, tongue slips under enclosure lip
            // Tongue extends back into lip ridge area for strength
            tongue_back_x = tongue_transition_x - tongue_extension + lip_ridge_thickness;

            // Left tongue assembly (low Y side)
            tongue_width_left = eth_y_start - wall_thickness - lid_tolerance - 0.5;
            // Vertical part - extends back into lip ridge for strength
            translate([tongue_back_x,
                       wall_thickness + lid_tolerance,
                       tongue_z])
                cube([tongue_extension, tongue_width_left, -tongue_z]);
            // Horizontal tongue - extends outward toward wall
            translate([tongue_transition_x + lip_ridge_thickness,
                       wall_thickness + lid_tolerance,
                       tongue_z])
                cube([lip_overhang, tongue_width_left, tongue_thickness]);

            // Right tongue assembly (high Y side)
            tongue_width_right = outer_width - eth_y_end - wall_thickness - lid_tolerance - 0.5;
            // Vertical part - extends back into lip ridge for strength
            translate([tongue_back_x,
                       eth_y_end + 0.5,
                       tongue_z])
                cube([tongue_extension, tongue_width_right, -tongue_z]);
            // Horizontal tongue
            translate([tongue_transition_x + lip_ridge_thickness,
                       eth_y_end + 0.5,
                       tongue_z])
                cube([lip_overhang, tongue_width_right, tongue_thickness]);
        }

        // Cutout in lip ridge at Ethernet end - lip only, not lid plate
        eth_cutout_width = eth_port_width + eth_port_clearance * 2 + 2;
        eth_lip_y = (external_width - eth_cutout_width) / 2;
        translate([external_length - wall_thickness - 10, eth_lip_y, -lid_lip_height])
            cube([15, eth_cutout_width, lid_lip_height]);

        // Screw holes and lip clearance for screw posts - only 2 at back end
        for (y = [post_y_inset, external_width - post_y_inset]) {
            // Clearance in lip ridge for screw posts
            translate([post_x, y, -lid_lip_height - 0.1])
                cylinder(d=screw_post_diameter + 1, h=lid_lip_height + 0.2, $fn=24);
            // Countersunk holes from top
            translate([post_x, y, -lid_lip_height - 1])
                cylinder(d=screw_hole_diameter, h=lid_height + 2, $fn=24);
            // Countersink
            translate([post_x, y, wall_thickness - 1.5])
                cylinder(d1=screw_hole_diameter, d2=screw_hole_diameter + 3, h=2, $fn=24);
        }

        // Ventilation slots (optional)
        slot_width = 2;
        slot_length = 20;
        slot_spacing = 5;
        num_slots = 3;

        for (i = [0:num_slots-1]) {
            translate([outer_length/2 - slot_length/2,
                      outer_width/2 - (num_slots-1)*slot_spacing/2 + i*slot_spacing,
                      -1])
                hull() {
                    translate([0, 0, 0]) cylinder(d=slot_width, h=wall_thickness+2, $fn=16);
                    translate([slot_length, 0, 0]) cylinder(d=slot_width, h=wall_thickness+2, $fn=16);
                }
        }
    }
}

// Board mockup for visualization
module board_mockup() {
    board_y = wall_thickness + clearance;
    board_z = wall_thickness + rail_height;  // Sitting on rails

    color("green", 0.7)
    translate([board_start_x, board_y, board_z]) {
        // PCB
        cube([board_length, board_width, board_thickness]);

        // Ethernet jack representation (RJ45 is narrower than board, centered)
        // Standard RJ45: ~21mm deep, ~16mm wide, ~13.5mm tall
        color("silver", 0.8)
        translate([board_length - 21, (board_width - eth_port_width)/2, board_thickness])
            cube([21, eth_port_width, eth_port_height]);

        // ESP32 module representation (WT32-S1)
        color("darkgray", 0.8)
        translate([5, (board_width - 18)/2, board_thickness])
            cube([25, 18, 3]);
    }
}

// ============================================
// RENDER OPTIONS - Comment/uncomment as needed
// ============================================

// Show assembled view
module assembled_view() {
    enclosure_base();

    // Lid in position (raised slightly for visibility)
    translate([0, 0, external_height + 5])
        enclosure_lid();

    // Board mockup
    board_mockup();
}

// Print layout - parts separated for 3D printing
module print_layout() {
    enclosure_base();

    // Lid positioned next to base for printing
    translate([external_length + 20, 0, wall_thickness])
        rotate([180, 0, 0])
            enclosure_lid();
}

// ============================================
// OUTPUT - Choose what to render
// ============================================

// Uncomment ONE of the following:

//assembled_view();      // For visualization
 print_layout();     // For 3D printing
// enclosure_base();   // Base only
// enclosure_lid();    // Lid only

// ============================================
// DIMENSIONS OUTPUT
// ============================================
echo("=== WT32-ETH01 Enclosure Dimensions ===");
echo(str("External dimensions: ", external_length, " x ", external_width, " x ", external_height, " mm"));
echo(str("Wire hole diameter: ", wire_hole_diameter, " mm"));
echo(str("Floor mount hole diameter: ", floor_mount_hole_diameter, " mm"));
echo(str("Floor mount hole spacing: ", board_length * 0.5, " mm apart"));
