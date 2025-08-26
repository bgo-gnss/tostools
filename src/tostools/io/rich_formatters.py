#!/usr/bin/env python3
"""
Rich table formatters for GPS station data visualization.

Provides enhanced table formatting with colors, compact columns, and flexible display options.
"""

from typing import Dict, List, Any, Optional
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from datetime import datetime


class GPSStationFormatter:
    """Rich table formatter for GPS station data.
    
    TODO: Add support for customizable color themes
    TODO: Implement export to HTML/PDF formats
    """
    
    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console(force_terminal=True, width=200)
        
        # Column name mappings: short_name -> (full_name, color_style)
        self.static_columns = {
            "Name": ("Station Name", None),
            "Lat": ("Latitude", "cyan"),
            "Lon": ("Longitude", "cyan"), 
            "Alt": ("Altitude", "green"),
            "Geol": ("Geological Characteristic", None),
            "Bedrock": ("Bedrock Type", None),
            "Condition": ("Bedrock Condition", None),
            "Near Fault": ("Near Fault Zones", "yellow"),
            "Network": ("In Network EPOS", None),
            "Marker": ("Marker", None),
            "Start": ("Date Start", "blue"),
        }
        
        # History columns with grouped headers and color coding
        self.history_columns = [
            # Time columns
            ("From", "Time From", "blue", None),
            ("To", "Time To", "blue", None),
            # Receiver group - green
            ("Type", "Receiver Type", "green", "Receiver"),
            ("SN", "Receiver SN", "green", "Receiver"),
            ("FW", "Receiver FW", "green", "Receiver"),
            ("SW", "Receiver SW", "green", "Receiver"),
            # Antenna group - red  
            ("Type", "Antenna Type", "red", "Antenna"),
            ("SN", "Antenna SN", "red", "Antenna"),
            ("Height", "Antenna Height", "red", "Antenna"),
            ("East", "Antenna East", "red", "Antenna"),
            ("North", "Antenna North", "red", "Antenna"),
            ("Radome", "Radome", "red", "Antenna"),
            # Monument group - yellow
            ("Height", "Monument Height", "yellow", "Monument"),
            ("East", "Monument East", "yellow", "Monument"), 
            ("North", "Monument North", "yellow", "Monument"),
        ]

    def print_static_data(self, station: Dict[str, Any]) -> None:
        """Print static station information in a compact table."""
        table = Table(title=f"Station: {station.get('name', 'Unknown')}", 
                     title_style="bold blue",
                     box=box.ROUNDED)
        
        # Add columns
        table.add_column("Property", style="bold")
        table.add_column("Value", style="cyan")
        
        # Add static data rows - marker first as requested
        rows = [
            ("Marker", station.get("marker", "N/A").upper() if station.get("marker", "N/A") != "N/A" else "N/A"),
            ("Name", station.get("name", "N/A")),
            ("IERS DOMES Number", station.get("iers_domes_number", "N/A")),
            ("Coordinates", f"{station.get('lat', 'N/A')}, {station.get('lon', 'N/A')}"),
            ("Altitude", f"{station.get('altitude', 'N/A')} m"),
            ("Geological", station.get("geological_characteristic", "N/A")),
            ("Bedrock Type", station.get("bedrock_type", "N/A")),
            ("Bedrock Condition", station.get("bedrock_condition", "N/A")),
            ("Near Fault", "Yes" if station.get("is_near_fault_zones") == "já" else "No"),
            ("EPOS Network", "Yes" if station.get("in_network_epos") else "No"),
            ("Date Started", station.get("date_start", "N/A")),
        ]
        
        for prop, value in rows:
            table.add_row(prop, str(value))
            
        self.console.print(table)

    def print_contact_summary(self, station: Dict[str, Any]) -> None:
        """Print brief contact information."""
        if "contact" not in station:
            return
            
        table = Table(title="Contacts", title_style="bold green", box=box.SIMPLE)
        table.add_column("Role", style="bold")
        table.add_column("Name", style="cyan")
        
        # Deduplicate contacts by name and role
        seen_contacts = set()
        for contact_id, contact_info in station["contact"].items():
            role = contact_info.get("role", contact_info.get("role_is", "Unknown"))
            name = contact_info.get("name", "N/A")
            contact_key = (role.lower(), name.lower())
            
            if contact_key not in seen_contacts:
                seen_contacts.add(contact_key)
                table.add_row(role.title(), name)
            
        self.console.print(table)

    def print_detailed_contacts(self, station: Dict[str, Any]) -> None:
        """Print detailed contact information in English and Icelandic."""
        if "contact" not in station:
            self.console.print("[yellow]No contact information available[/yellow]")
            return
            
        self.console.print("\n[bold white]Contact Information[/bold white]")
        self.console.print()
            
        for i, (contact_id, contact_info) in enumerate(station["contact"].items()):
            # Create compact table with proper column widths
            contact_table = Table(
                box=box.ROUNDED,
                show_header=True,
                header_style="bold white",
                padding=(0, 1),
                width=90,  # Limit table width to be more compact
                expand=False  # Don't expand to fill terminal
            )
            contact_table.add_column("Field", style="bold", width=10, no_wrap=True)
            contact_table.add_column("English", style="cyan", width=35, max_width=35) 
            contact_table.add_column("Icelandic", style="yellow", width=30, max_width=30)
            
            # Get contact info with better field mapping
            role_en = contact_info.get("role", "N/A")
            role_is = contact_info.get("role_is", "N/A")
            name = contact_info.get("name", "N/A")
            email = contact_info.get("email", "")
            phone = contact_info.get("phone_primary", contact_info.get("phone", ""))
            address = contact_info.get("address_en", contact_info.get("address", ""))
            address_is = contact_info.get("address", "N/A")
            
            # Add contact fields with better organization
            fields = [
                ("Role", role_en, role_is),
                ("Name", name, name),  # Name is usually the same
                ("Email", email or "N/A", email or "N/A"),
                ("Phone", phone or "N/A", phone or "N/A"),
                ("Address", address or "N/A", address_is if address_is != address else "N/A"),
            ]
            
            for field, eng_val, ice_val in fields:
                # Only show rows with meaningful data
                if eng_val and eng_val != "N/A":
                    contact_table.add_row(field, eng_val, ice_val)
            
            # Print contact with clean title
            title = f"[bold green]Contact {i+1}: {role_en.title() if role_en != 'N/A' else 'Unknown'}[/bold green]"
            self.console.print(title)
            self.console.print(contact_table)
            
            # Add spacing between contacts
            if i < len(station["contact"]) - 1:
                self.console.print()

    def print_device_history(self, station: Dict[str, Any]) -> None:
        """Print device history with grouped headers and color-coded columns."""
        if "device_history" not in station or not station["device_history"]:
            self.console.print("[yellow]No device history available[/yellow]")
            return
        
        # Print centered title and group headers with minimal spacing  
        self.console.print("[bold white]                                           Station History[/bold white]")
        # FIXME: Fine-tune group header alignment - Antenna/Monument headers still slightly off
        self.console.print("                      [bold green]Receiver[/bold green]                           [bold red]Antenna[/bold red]                                    [bold yellow]Monument[/bold yellow]")
        table = Table(box=box.SIMPLE_HEAVY,
                     border_style="dim",
                     header_style="bold white",
                     show_header=True,
                     min_width=None,
                     padding=(0, 0),
                     pad_edge=False,
                     collapse_padding=True)
        
        # Add columns with exact widths for actual data - compact but no truncation
        table.add_column("From", style="blue", justify="left", width=10, no_wrap=True)  # "2001-07-19"
        table.add_column("To", style="blue", justify="left", width=10, no_wrap=True)    # "2002-03-29" or "Present"
        # Receiver columns - green 
        table.add_column("Type", style="green", justify="left", width=13, no_wrap=True)  # "ASHTECH UZ-12", "TRIMBLE NETR9" 
        table.add_column("SN", style="green", justify="left", width=10, no_wrap=True)     # "633Z024", "5038K70713"
        table.add_column("FW", style="green", justify="right", width=8, no_wrap=True)    # "UFB1", "CJ00", "NP 4.60"
        table.add_column("SW", style="green", justify="right", width=4, no_wrap=True)     # "9.93", "4.60"
        # Antenna columns - red
        table.add_column("Type", style="red", justify="left", width=12, no_wrap=True)     # "ASH701945C_M", "TRM57971.00"
        table.add_column("Radome", style="red", justify="left", width=6, no_wrap=True)    # "SCIS", "N/A"
        table.add_column("SN", style="red", justify="left", width=10, no_wrap=True)       # "1999040150", "1441045161"
        table.add_column("Height", style="red", justify="right", width=7, no_wrap=True)   # "0.000", "-0.007"
        table.add_column("East", style="red", justify="right", width=6, no_wrap=True)     # "0.000"
        table.add_column("North", style="red", justify="right", width=6, no_wrap=True)    # "0.000"
        # Monument columns - yellow
        table.add_column("Height", style="yellow", justify="right", width=7, no_wrap=True)  # "1.014"
        table.add_column("East", style="yellow", justify="right", width=6, no_wrap=True)    # "0.000"
        table.add_column("North", style="yellow", justify="right", width=6, no_wrap=True)   # "0.000"
        
        # Add data rows
        for device_session in station["device_history"]:
            row_data = []
            
            # Time period - format dates to be compact
            time_from = device_session.get("time_from", "N/A")
            time_to = device_session.get("time_to", "Present")
            
            # Format dates more compactly
            if time_from != "N/A":
                time_from = str(time_from)[:10]  # Just YYYY-MM-DD
            if time_to != "Present" and time_to != "None" and time_to:
                time_to = str(time_to)[:10]  # Just YYYY-MM-DD  
            elif time_to == "None" or not time_to:
                time_to = "Present"
                
            row_data.extend([time_from, time_to])
            
            # GNSS Receiver
            receiver = device_session.get("gnss_receiver", {})
            fw_version = self._safe_get(receiver.get("firmware_version"))
            # Keep firmware version full length up to 8 characters
            if fw_version != "N/A" and len(fw_version) > 8:
                fw_version = fw_version[:8]
            
            row_data.extend([
                self._safe_get(receiver.get("model")),
                self._safe_get(receiver.get("serial_number")),
                fw_version,
                self._safe_get(receiver.get("software_version"))
            ])
            
            # Antenna + Radome
            antenna = device_session.get("antenna", {})
            radome = device_session.get("radome", {})
            
            # Format numeric values for decimal alignment
            height = self._format_numeric(antenna.get("antenna_height", "N/A"))
            east = self._format_numeric(antenna.get("antenna_offset_east", "N/A"))
            north = self._format_numeric(antenna.get("antenna_offset_north", "N/A"))
            
            row_data.extend([
                self._safe_get(antenna.get("model")),
                self._safe_get_radome(radome.get("model")),
                self._safe_get(antenna.get("serial_number")),
                height,
                east,
                north
            ])
            
            # Monument
            monument = device_session.get("monument", {})
            mon_height = self._format_numeric(monument.get("monument_height", "N/A"))
            mon_east = self._format_numeric(monument.get("monument_offset_east", "N/A"))
            mon_north = self._format_numeric(monument.get("monument_offset_north", "N/A"))
            
            row_data.extend([mon_height, mon_east, mon_north])
            
            table.add_row(*row_data)
            
        self.console.print(table)
    
    def _is_numeric_column(self, column_name: str) -> bool:
        """Check if column should be right-aligned for numeric data."""
        numeric_columns = {"Height", "East", "North", "FW", "SW"}
        return column_name in numeric_columns
    
    def _get_group_color(self, group: str) -> str:
        """Get color for group headers."""
        colors = {"Receiver": "green", "Antenna": "red", "Monument": "yellow"}
        return colors.get(group, "white")
    
    def _safe_get(self, value) -> str:
        """Convert empty/None values to N/A."""
        if value is None or value == "" or value == "None":
            return "N/A"
        return str(value)
    
    def _safe_get_radome(self, value) -> str:
        """Convert empty/None radome values to NONE (IGS standard)."""
        if value is None or value == "" or value == "None":
            return "NONE"
        return str(value)
    
    def _format_numeric(self, value) -> str:
        """Format numeric values for decimal alignment."""
        if value == "N/A" or value is None or value == "" or value == "None":
            return "N/A"
        try:
            # Convert to float and format with consistent decimal places
            num_val = float(value)
            if num_val == int(num_val):
                return f"{int(num_val)}.000"
            else:
                return f"{num_val:.3f}"
        except (ValueError, TypeError):
            return str(value) if value else "N/A"

    def print_station_complete(self, station: Dict[str, Any], 
                              show_static: bool = True,
                              show_contacts: bool = True, 
                              show_history: bool = True) -> None:
        """Print complete station information with flexible display options."""
        if show_static:
            self.print_static_data(station)
            
        if show_contacts:
            self.print_contact_summary(station)
            
        if show_history:
            self.print_device_history(station)


def print_stations_rich(stations: List[Dict[str, Any]], 
                       show_static: bool = True,
                       show_contacts: bool = True,
                       show_history: bool = True,
                       detailed_contacts: bool = False) -> None:
    """
    Print stations using rich formatting.
    
    Args:
        stations: List of station dictionaries
        show_static: Whether to show static station data
        show_contacts: Whether to show contact summary
        show_history: Whether to show device history
        detailed_contacts: Whether to show detailed contact information
    """
    formatter = GPSStationFormatter()
    
    for i, station in enumerate(stations):
        if i > 0:
            formatter.console.print("─" * 80, style="dim")
            formatter.console.print()
        
        if detailed_contacts:
            formatter.print_detailed_contacts(station)
        else:
            formatter.print_station_complete(station, show_static, show_contacts, show_history)