$(document).ready(function() {
    function changeSystemForm( value, animate ) {
        if ( animate ) {
            delay = 500;
            speed = 'slow';
        } else {
            delay = 0;
            speed = 'fast';
        }
        if ( value == 'static_interface' ) {
            setTimeout( function() {
                $('#static-form').slideDown(speed);
            }, delay);
            $('#dynamic-form').slideUp();
        } else {
            setTimeout( function() {
                $('#dynamic-form').slideDown(speed);
            }, delay);
            $('#static-form').slideUp();
        }
    }

    jQuery.each( $("input[name='interface_type']:checked"), function() {
        changeSystemForm( this.value, false );
    });
    $("input[name='interface_type']").change( function() {
        changeSystemForm( this.value, true );
    });

    $( document ).on('submit', '#system-form form', function( e ) {
        e.preventDefault();
        var fields;
        $(this).find('.error').remove();

        if ($("input[name=interface_type]:checked").val() === undefined) {
            $("label[for=id_interface_type_0]:first").after(
                '<p class="error">This field is required.</p>');
            return false;
        } else {
            fields = $(this).find(':input:visible').serializeArray();
        }

        var url = '/core/system/create/';
        var csrfToken = $('#view-metadata').attr( 'data-csrfToken' );
        $.when( ajax_form_submit( url, fields, csrfToken ) ).done( function( ret_data ) {
            location.href = '/core/system/' + ret_data.system_id.toString();
        })
    });
});
