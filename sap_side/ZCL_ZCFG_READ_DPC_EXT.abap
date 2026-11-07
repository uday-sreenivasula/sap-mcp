"! ZCFG_READ_SRV - read-only table reader for sap-mcp (Claude).
"!
"! GET function imports:
"!   ReadTable( TableName, Where, Fields, MaxRows, Skip ) -> TableRows
"!   GetTableFields( TableName )                           -> TableFields
"!   Compute( Operation, Params )                          -> TableRows
"!     One entry point for everything computed inside SAP. Params is a JSON
"!     object with UPPER-CASE keys; each result row is a JSON object in Data.
"!     AGGREGATE        SUM/AVG/MIN/MAX/COUNT/COUNTD + GROUP BY, ORDER BY, top N
"!     DUPLICATES       groups of rows sharing the same values on chosen fields
"!     TRIAL_BALANCE    ACDOCA per G/L account: opening, debit, credit, closing
"!     AGEING           vendor/customer open items at a key date, due buckets
"!     GRIR             EKBE goods receipt vs invoice receipt per PO item
"!     LIST_REPORTS     executable reports matching REPORT (pattern, default Z*)
"!     REPORT_INFO      a report's selection fields and saved variants
"!     RUN_REPORT       SUBMIT ... AND RETURN with a variant and/or selections;
"!                      ALV output is captured as data (CL_SALV_BS_RUNTIME_INFO),
"!                      classic lists as text lines. Any executable report can
"!                      run, so a report that posts or changes data will do so.
"!     LIST_ATTACHMENTS / GET_ATTACHMENT   GOS files (SRGBTBREL -> SOOD;
"!       content via SO_DOCUMENT_READ_API1, base64). GET_ATTACHMENT only
"!       returns a document linked to the object named in the call.
"!     Every operation checks S_TABU_NAM for the tables it reads.
"!
"! Security model
"!   * Read only: the only database access is one dynamic SELECT.
"!   * AUTHORITY-CHECK S_TABU_NAM (ACTVT 03, TABLE = requested table) for
"!     every ReadTable call, under the caller's own SAP user.
"!   * WHERE is checked so it cannot reach other tables: no sub-queries,
"!     no FROM/JOIN/UNION/EXISTS, no host variables (@), no backquotes,
"!     balanced quotes only. Field names are checked against the DDIC.
"!   * A short list of credential/secret tables is always refused.
"!   * Client-dependent tables are read in the logon client only.
"!   * At most 5000 rows per call.
"!
"! Replace the whole generated source of ZCL_ZCFG_READ_DPC_EXT with this
"! (SE24 -> "Source Code-Based" view, or Eclipse ADT).
CLASS zcl_zcfg_read_dpc_ext DEFINITION
  PUBLIC
  INHERITING FROM zcl_zcfg_read_dpc
  CREATE PUBLIC.

  PUBLIC SECTION.
    METHODS /iwbep/if_mgw_appl_srv_runtime~execute_action REDEFINITION.

  PROTECTED SECTION.

  PRIVATE SECTION.
    " SEGW declares TT_TABLEROW / TT_TABLEFIELD without a key, which makes
    " them generic and not allowed as RETURNING types; these are fully typed.
    TYPES:
      tt_rows   TYPE STANDARD TABLE OF zcl_zcfg_read_mpc=>ts_tablerow WITH EMPTY KEY,
      tt_fields TYPE STANDARD TABLE OF zcl_zcfg_read_mpc=>ts_tablefield WITH EMPTY KEY.

    " Parameters of Compute, deserialized from the Params JSON (UPPER-CASE keys).
    TYPES:
      BEGIN OF ts_params,
        table_name   TYPE string,
        where_cond   TYPE string,
        fields       TYPE string,
        group_by     TYPE string,
        measures     TYPE string,
        order_by     TYPE string,
        max_rows     TYPE i,
        min_count    TYPE i,
        company_code TYPE string,
        ledger       TYPE string,
        fiscal_year  TYPE string,
        period_from  TYPE i,
        period_to    TYPE i,
        account_from TYPE string,
        account_to   TYPE string,
        account_type TYPE string,
        key_date     TYPE string,
        partner      TYPE string,
        vendor       TYPE string,
        po           TYPE string,
        object_type  TYPE string,
        object_key   TYPE string,
        doc_id       TYPE string,
        report       TYPE string,
        variant      TYPE string,
        selections   TYPE rsparams_tt,
      END OF ts_params.

    CONSTANTS:
      c_default_rows TYPE i VALUE 100,
      c_max_rows     TYPE i VALUE 5000,
      c_max_where    TYPE i VALUE 4000,
      c_max_file     TYPE i VALUE 15000000.   " bytes per GET_ATTACHMENT call

    METHODS read_table
      IMPORTING it_parameter   TYPE /iwbep/t_mgw_name_value_pair
      RETURNING VALUE(rt_rows) TYPE tt_rows
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS get_table_fields
      IMPORTING it_parameter     TYPE /iwbep/t_mgw_name_value_pair
      RETURNING VALUE(rt_fields) TYPE tt_fields
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS compute
      IMPORTING it_parameter   TYPE /iwbep/t_mgw_name_value_pair
      RETURNING VALUE(rt_rows) TYPE tt_rows
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS op_aggregate
      IMPORTING is_p           TYPE ts_params
                iv_duplicates  TYPE abap_bool DEFAULT abap_false
      RETURNING VALUE(rt_rows) TYPE tt_rows
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS op_trial_balance
      IMPORTING is_p           TYPE ts_params
      RETURNING VALUE(rt_rows) TYPE tt_rows
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS op_ageing
      IMPORTING is_p           TYPE ts_params
      RETURNING VALUE(rt_rows) TYPE tt_rows
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS op_grir
      IMPORTING is_p           TYPE ts_params
      RETURNING VALUE(rt_rows) TYPE tt_rows
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS op_reports
      IMPORTING is_p           TYPE ts_params
                iv_op          TYPE string
      RETURNING VALUE(rt_rows) TYPE tt_rows
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS resolve_report
      IMPORTING iv_name        TYPE syrepid
      RETURNING VALUE(rv_prog) TYPE syrepid
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS op_attachments
      IMPORTING is_p            TYPE ts_params
                iv_with_content TYPE abap_bool
      RETURNING VALUE(rt_rows)  TYPE tt_rows
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS need_table
      IMPORTING iv_table TYPE tabname
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS to_row
      IMPORTING iv_no         TYPE i
                is_data       TYPE any
      RETURNING VALUE(rs_row) TYPE zcl_zcfg_read_mpc=>ts_tablerow.

    METHODS field_list
      IMPORTING iv_text        TYPE string
      RETURNING VALUE(rt_list) TYPE string_table.

    METHODS attachment_content
      IMPORTING iv_doc_id      TYPE string
                iv_size        TYPE i
      EXPORTING ev_file_name   TYPE string
                ev_base64      TYPE string
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS param
      IMPORTING it_parameter    TYPE /iwbep/t_mgw_name_value_pair
                iv_name         TYPE string
      RETURNING VALUE(rv_value) TYPE string.

    METHODS int_param
      IMPORTING it_parameter    TYPE /iwbep/t_mgw_name_value_pair
                iv_name         TYPE string
                iv_default      TYPE i
      RETURNING VALUE(rv_value) TYPE i.

    METHODS check_table
      IMPORTING iv_table TYPE tabname
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS check_where
      IMPORTING iv_where TYPE string
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS ddic_fields
      IMPORTING iv_table        TYPE tabname
      RETURNING VALUE(rt_dfies) TYPE ddfields
      RAISING   /iwbep/cx_mgw_busi_exception.

    METHODS fail
      IMPORTING iv_text TYPE csequence
      RAISING   /iwbep/cx_mgw_busi_exception.
ENDCLASS.



CLASS zcl_zcfg_read_dpc_ext IMPLEMENTATION.

  METHOD /iwbep/if_mgw_appl_srv_runtime~execute_action.
    CASE iv_action_name.
      WHEN 'ReadTable'.
        DATA(lt_rows) = read_table( it_parameter ).
        copy_data_to_ref( EXPORTING is_data = lt_rows
                          CHANGING  cr_data = er_data ).

      WHEN 'GetTableFields'.
        DATA(lt_fields) = get_table_fields( it_parameter ).
        copy_data_to_ref( EXPORTING is_data = lt_fields
                          CHANGING  cr_data = er_data ).

      WHEN 'Compute'.
        DATA(lt_out) = compute( it_parameter ).
        copy_data_to_ref( EXPORTING is_data = lt_out
                          CHANGING  cr_data = er_data ).

      WHEN OTHERS.
        super->/iwbep/if_mgw_appl_srv_runtime~execute_action(
          EXPORTING iv_action_name          = iv_action_name
                    it_parameter            = it_parameter
                    io_tech_request_context = io_tech_request_context
          IMPORTING er_data                 = er_data ).
    ENDCASE.
  ENDMETHOD.


  METHOD read_table.
    DATA: lv_table   TYPE tabname,
          lt_wanted  TYPE string_table,
          lt_unknown TYPE string_table,
          lt_select  TYPE string_table,
          lt_order   TYPE string_table,
          lt_comp    TYPE cl_abap_structdescr=>component_table,
          lo_line    TYPE REF TO cl_abap_structdescr,
          lo_tab     TYPE REF TO cl_abap_tabledescr,
          lr_data    TYPE REF TO data.
    FIELD-SYMBOLS <lt_data> TYPE STANDARD TABLE.

    lv_table = to_upper( condense( param( it_parameter = it_parameter iv_name = `TableName` ) ) ).
    IF lv_table IS INITIAL.
      fail( `TableName is required.` ).
    ENDIF.
    check_table( lv_table ).

    " Not condensed: that would also squash spaces inside quoted literals.
    DATA(lv_where) = param( it_parameter = it_parameter iv_name = `Where` ).
    check_where( lv_where ).

    DATA(lv_max) = int_param( it_parameter = it_parameter iv_name = `MaxRows` iv_default = c_default_rows ).
    lv_max = nmin( val1 = nmax( val1 = lv_max val2 = 1 ) val2 = c_max_rows ).
    DATA(lv_skip) = nmax( val1 = int_param( it_parameter = it_parameter iv_name = `Skip` iv_default = 0 )
                          val2 = 0 ).

    " Requested fields: comma and/or space separated, empty = all fields.
    DATA(lv_fields) = to_upper( translate( val  = param( it_parameter = it_parameter iv_name = `Fields` )
                                           from = ` ` to = `,` ) ).
    SPLIT lv_fields AT ',' INTO TABLE lt_wanted.
    DELETE lt_wanted WHERE table_line IS INITIAL.
    SORT lt_wanted.
    DELETE ADJACENT DUPLICATES FROM lt_wanted.

    DATA(lt_dfies) = ddic_fields( lv_table ).

    LOOP AT lt_wanted INTO DATA(lv_wanted).
      IF NOT line_exists( lt_dfies[ fieldname = lv_wanted ] ).
        APPEND lv_wanted TO lt_unknown.
      ENDIF.
    ENDLOOP.
    IF lt_unknown IS NOT INITIAL.
      fail( |Unknown field(s) in { lv_table }: { concat_lines_of( table = lt_unknown sep = `, ` ) }| ).
    ENDIF.

    " Select key fields (always) plus the requested ones. The client field is
    " left out entirely: the logon client is applied automatically, and strict
    " SQL refuses a selected column with no matching field in the result.
    LOOP AT lt_dfies INTO DATA(ls_dfies).
      IF ls_dfies-datatype = 'CLNT'.
        CONTINUE.
      ENDIF.
      DATA(lv_name) = CONV string( ls_dfies-fieldname ).
      IF ls_dfies-keyflag <> abap_true
         AND lt_wanted IS NOT INITIAL
         AND NOT line_exists( lt_wanted[ table_line = lv_name ] ).
        CONTINUE.
      ENDIF.
      APPEND lv_name TO lt_select.
      IF ls_dfies-keyflag = abap_true.
        APPEND lv_name TO lt_order.
      ENDIF.
      APPEND VALUE #( name = lv_name
                      type = CAST cl_abap_datadescr(
                               cl_abap_typedescr=>describe_by_name( |{ lv_table }-{ lv_name }| ) ) )
        TO lt_comp.
    ENDLOOP.
    IF lt_order IS INITIAL.        " keyless view: any stable order will do
      lt_order = lt_select.
    ENDIF.

    TRY.
        lo_line = cl_abap_structdescr=>create( p_components = lt_comp ).
        lo_tab  = cl_abap_tabledescr=>create( p_line_type = lo_line ).
      CATCH cx_sy_struct_creation cx_sy_table_creation INTO DATA(lx_rtti).
        fail( |Cannot build result structure for { lv_table }: { lx_rtti->get_text( ) }| ).
    ENDTRY.
    CREATE DATA lr_data TYPE HANDLE lo_tab.
    ASSIGN lr_data->* TO <lt_data>.

    DATA(lv_select) = concat_lines_of( table = lt_select sep = `, ` ).
    DATA(lv_order)  = concat_lines_of( table = lt_order  sep = `, ` ).

    TRY.
        SELECT (lv_select)
          FROM (lv_table)
          WHERE (lv_where)
          ORDER BY (lv_order)
          INTO CORRESPONDING FIELDS OF TABLE @<lt_data>
          UP TO @lv_max ROWS
          OFFSET @lv_skip.
      CATCH cx_sy_dynamic_osql_error INTO DATA(lx_sql).
        fail( |Invalid query on { lv_table }: { lx_sql->get_text( ) }| ).
      CATCH cx_sy_open_sql_db INTO DATA(lx_db).
        fail( |Database error on { lv_table }: { lx_db->get_text( ) }| ).
    ENDTRY.

    LOOP AT <lt_data> ASSIGNING FIELD-SYMBOL(<ls_row>).
      DATA(lv_row_no) = lv_skip + sy-tabix.
      APPEND VALUE #( row_no = lv_row_no
                      data   = /ui2/cl_json=>serialize( data           = <ls_row>
                                                        numc_as_string = abap_true ) )
        TO rt_rows.
    ENDLOOP.
  ENDMETHOD.


  METHOD compute.
    DATA ls_p TYPE ts_params.

    DATA(lv_op)   = to_upper( condense( param( it_parameter = it_parameter iv_name = `Operation` ) ) ).
    DATA(lv_json) = param( it_parameter = it_parameter iv_name = `Params` ).
    IF lv_json IS NOT INITIAL.
      TRY.
          /ui2/cl_json=>deserialize( EXPORTING json = lv_json
                                     CHANGING  data = ls_p ).
        CATCH cx_root INTO DATA(lx_json).
          fail( |Params is not valid JSON: { lx_json->get_text( ) }| ).
      ENDTRY.
    ENDIF.
    IF ls_p-max_rows <= 0.
      ls_p-max_rows = c_default_rows.
    ENDIF.
    ls_p-max_rows = nmin( val1 = ls_p-max_rows val2 = c_max_rows ).

    CASE lv_op.
      WHEN 'AGGREGATE'.
        rt_rows = op_aggregate( ls_p ).
      WHEN 'DUPLICATES'.
        rt_rows = op_aggregate( is_p = ls_p iv_duplicates = abap_true ).
      WHEN 'TRIAL_BALANCE'.
        rt_rows = op_trial_balance( ls_p ).
      WHEN 'AGEING'.
        rt_rows = op_ageing( ls_p ).
      WHEN 'GRIR'.
        rt_rows = op_grir( ls_p ).
      WHEN 'LIST_REPORTS' OR 'REPORT_INFO' OR 'RUN_REPORT'.
        rt_rows = op_reports( is_p = ls_p iv_op = lv_op ).
      WHEN 'LIST_ATTACHMENTS'.
        rt_rows = op_attachments( is_p = ls_p iv_with_content = abap_false ).
      WHEN 'GET_ATTACHMENT'.
        rt_rows = op_attachments( is_p = ls_p iv_with_content = abap_true ).
      WHEN OTHERS.
        fail( |Unknown Operation '{ lv_op }'. Use AGGREGATE, DUPLICATES, TRIAL_BALANCE, AGEING, GRIR, | &&
              |LIST_REPORTS, REPORT_INFO, RUN_REPORT, LIST_ATTACHMENTS or GET_ATTACHMENT.| ).
    ENDCASE.
  ENDMETHOD.


  METHOD op_aggregate.
    " AGGREGATE: measures + group by. DUPLICATES: group by FIELDS, keep groups
    " with at least MIN_COUNT (default 2) rows, most frequent first.
    DATA: lv_table  TYPE tabname,
          lt_select TYPE string_table,
          lt_comp   TYPE cl_abap_structdescr=>component_table,
          lt_meas   TYPE string_table,
          lt_alias  TYPE string_table,
          lo_line   TYPE REF TO cl_abap_structdescr,
          lo_tab    TYPE REF TO cl_abap_tabledescr,
          lr_data   TYPE REF TO data,
          lv_having TYPE string,
          lv_order  TYPE string.
    FIELD-SYMBOLS <lt_data> TYPE STANDARD TABLE.

    lv_table = to_upper( condense( is_p-table_name ) ).
    IF lv_table IS INITIAL.
      fail( `TABLE_NAME is required.` ).
    ENDIF.
    check_table( lv_table ).
    check_where( is_p-where_cond ).
    DATA(lt_dfies) = ddic_fields( lv_table ).

    DATA(lt_group) = field_list( COND #( WHEN iv_duplicates = abap_true THEN is_p-fields
                                         ELSE is_p-group_by ) ).
    IF iv_duplicates = abap_true AND lt_group IS INITIAL.
      fail( `DUPLICATES needs FIELDS, e.g. "LIFNR,XBLNR,RMWWR".` ).
    ENDIF.
    LOOP AT lt_group INTO DATA(lv_g).
      IF NOT line_exists( lt_dfies[ fieldname = lv_g ] ).
        fail( |Unknown field in { lv_table }: { lv_g }| ).
      ENDIF.
      APPEND lv_g TO lt_select.
      APPEND lv_g TO lt_alias.
      APPEND VALUE #( name = lv_g
                      type = CAST cl_abap_datadescr(
                               cl_abap_typedescr=>describe_by_name( |{ lv_table }-{ lv_g }| ) ) )
        TO lt_comp.
    ENDLOOP.

    lt_meas = COND #( WHEN iv_duplicates = abap_true THEN VALUE #( ( `COUNT` ) )
                      ELSE field_list( is_p-measures ) ).
    IF lt_meas IS INITIAL.
      lt_meas = VALUE #( ( `COUNT` ) ).
    ENDIF.
    LOOP AT lt_meas INTO DATA(lv_m).
      SPLIT lv_m AT ':' INTO DATA(lv_func) DATA(lv_field).
      " COUNT is an SQL keyword, so the row count is called ROW_COUNT.
      DATA(lv_alias) = COND string( WHEN lv_func = 'COUNT' THEN `ROW_COUNT`
                                    ELSE |{ lv_func }_{ lv_field }| ).
      IF strlen( lv_alias ) > 30.
        lv_alias = substring( val = lv_alias len = 30 ).
      ENDIF.
      IF lv_field IS NOT INITIAL AND NOT line_exists( lt_dfies[ fieldname = lv_field ] ).
        fail( |Unknown measure field in { lv_table }: { lv_field }| ).
      ENDIF.
      IF lv_func <> 'COUNT' AND lv_field IS INITIAL.
        fail( |{ lv_func } needs a field, e.g. { lv_func }:HSL.| ).
      ENDIF.
      CASE lv_func.
        WHEN 'COUNT'.
          APPEND |COUNT(*) AS { lv_alias }| TO lt_select.
          APPEND VALUE #( name = lv_alias type = cl_abap_elemdescr=>get_int8( ) ) TO lt_comp.
        WHEN 'COUNTD'.
          APPEND |COUNT( DISTINCT { lv_field } ) AS { lv_alias }| TO lt_select.
          APPEND VALUE #( name = lv_alias type = cl_abap_elemdescr=>get_int8( ) ) TO lt_comp.
        WHEN 'SUM'.
          " Strict SQL wants the target type of SUM to match the column's own type.
          APPEND |SUM( { lv_field } ) AS { lv_alias }| TO lt_select.
          APPEND VALUE #( name = lv_alias
                          type = CAST cl_abap_datadescr(
                                   cl_abap_typedescr=>describe_by_name( |{ lv_table }-{ lv_field }| ) ) )
            TO lt_comp.
        WHEN 'AVG'.
          APPEND |AVG( { lv_field } ) AS { lv_alias }| TO lt_select.      " AVG returns FLTP
          APPEND VALUE #( name = lv_alias type = cl_abap_elemdescr=>get_f( ) ) TO lt_comp.
        WHEN 'MIN' OR 'MAX'.
          APPEND |{ lv_func }( { lv_field } ) AS { lv_alias }| TO lt_select.
          APPEND VALUE #( name = lv_alias
                          type = CAST cl_abap_datadescr(
                                   cl_abap_typedescr=>describe_by_name( |{ lv_table }-{ lv_field }| ) ) )
            TO lt_comp.
        WHEN OTHERS.
          fail( |Unknown measure { lv_m }; use SUM:f, AVG:f, MIN:f, MAX:f, COUNT or COUNTD:f.| ).
      ENDCASE.
      APPEND lv_alias TO lt_alias.
    ENDLOOP.

    " ORDER_BY: "SUM_HSL DESC, LIFNR" - only result columns, ASC/DESC.
    IF iv_duplicates = abap_true.
      lv_order = `ROW_COUNT DESCENDING`.
      lv_having = |COUNT(*) >= { nmax( val1 = is_p-min_count val2 = 2 ) }|.
    ELSEIF is_p-order_by IS NOT INITIAL.
      DATA lt_ord TYPE string_table.
      DATA(lv_orderlist) = to_upper( is_p-order_by ).
      SPLIT lv_orderlist AT ',' INTO TABLE lt_ord.
      LOOP AT lt_ord INTO DATA(lv_o).
        lv_o = condense( lv_o ).
        SPLIT lv_o AT ` ` INTO DATA(lv_col) DATA(lv_dir).
        IF NOT line_exists( lt_alias[ table_line = lv_col ] ).
          fail( |ORDER_BY column { lv_col } is not in the result (use a group field or e.g. SUM_HSL).| ).
        ENDIF.
        lv_order = lv_order && COND string( WHEN lv_order IS INITIAL THEN `` ELSE `, ` ) && lv_col &&
                   COND string( WHEN lv_dir CP 'DESC*' THEN ` DESCENDING` ELSE ` ASCENDING` ).
      ENDLOOP.
    ELSEIF lt_group IS NOT INITIAL.
      lv_order = concat_lines_of( table = lt_group sep = `, ` ).
    ENDIF.

    TRY.
        lo_line = cl_abap_structdescr=>create( p_components = lt_comp ).
        lo_tab  = cl_abap_tabledescr=>create( p_line_type = lo_line ).
      CATCH cx_sy_struct_creation cx_sy_table_creation INTO DATA(lx_rtti).
        fail( |Cannot build result structure: { lx_rtti->get_text( ) }| ).
    ENDTRY.
    CREATE DATA lr_data TYPE HANDLE lo_tab.
    ASSIGN lr_data->* TO <lt_data>.

    DATA(lv_select) = concat_lines_of( table = lt_select sep = `, ` ).
    DATA(lv_group)  = concat_lines_of( table = lt_group  sep = `, ` ).
    DATA(lv_where)  = is_p-where_cond.
    DATA(lv_max)    = is_p-max_rows.

    TRY.
        IF lt_group IS INITIAL.
          SELECT (lv_select) FROM (lv_table) WHERE (lv_where)
            INTO CORRESPONDING FIELDS OF TABLE @<lt_data>.
        ELSEIF lv_having IS NOT INITIAL.
          SELECT (lv_select) FROM (lv_table) WHERE (lv_where)
            GROUP BY (lv_group) HAVING (lv_having) ORDER BY (lv_order)
            INTO CORRESPONDING FIELDS OF TABLE @<lt_data>
            UP TO @lv_max ROWS.
        ELSE.
          SELECT (lv_select) FROM (lv_table) WHERE (lv_where)
            GROUP BY (lv_group) ORDER BY (lv_order)
            INTO CORRESPONDING FIELDS OF TABLE @<lt_data>
            UP TO @lv_max ROWS.
        ENDIF.
      CATCH cx_sy_dynamic_osql_error INTO DATA(lx_sql).
        fail( |Invalid query on { lv_table }: { lx_sql->get_text( ) }| ).
      CATCH cx_sy_open_sql_db INTO DATA(lx_db).
        fail( |Database error on { lv_table }: { lx_db->get_text( ) }| ).
    ENDTRY.

    LOOP AT <lt_data> ASSIGNING FIELD-SYMBOL(<ls_row>).
      APPEND to_row( iv_no = sy-tabix is_data = <ls_row> ) TO rt_rows.
    ENDLOOP.
  ENDMETHOD.


  METHOD op_trial_balance.
    " Per G/L account from ACDOCA: opening (periods before PERIOD_FROM, incl. the
    " 000 balance carry-forward), period debit / credit, closing.
    TYPES: BEGIN OF ts_tb,
             racct   TYPE racct,
             txt50   TYPE txt50_skat,
             opening TYPE decfloat34,
             debit   TYPE decfloat34,
             credit  TYPE decfloat34,
             closing TYPE decfloat34,
           END OF ts_tb.
    DATA: lt_tb   TYPE SORTED TABLE OF ts_tb WITH UNIQUE KEY racct,
          lr_acct TYPE RANGE OF racct.

    need_table( 'ACDOCA' ).
    DATA(lv_bukrs)  = CONV bukrs( to_upper( is_p-company_code ) ).
    DATA(lv_gjahr)  = CONV gjahr( is_p-fiscal_year ).
    DATA(lv_ledger) = CONV fins_ledger( COND string( WHEN is_p-ledger IS INITIAL THEN `0L`
                                                     ELSE to_upper( is_p-ledger ) ) ).
    IF lv_bukrs IS INITIAL OR lv_gjahr IS INITIAL.
      fail( `TRIAL_BALANCE needs COMPANY_CODE and FISCAL_YEAR.` ).
    ENDIF.
    DATA(lv_from) = CONV poper( |{ nmax( val1 = is_p-period_from val2 = 1 ) WIDTH = 3 ALIGN = RIGHT PAD = '0' }| ).
    DATA(lv_to)   = CONV poper( |{ COND i( WHEN is_p-period_to <= 0 THEN 16 ELSE is_p-period_to )
                                   WIDTH = 3 ALIGN = RIGHT PAD = '0' }| ).
    IF is_p-account_from IS NOT INITIAL OR is_p-account_to IS NOT INITIAL.
      lr_acct = VALUE #( ( sign = 'I' option = 'BT'
                           low  = |{ is_p-account_from ALPHA = IN WIDTH = 10 }|
                           high = COND #( WHEN is_p-account_to IS INITIAL THEN '9999999999'
                                          ELSE |{ is_p-account_to ALPHA = IN WIDTH = 10 }| ) ) ).
    ENDIF.

    SELECT racct, SUM( hsl ) AS amount FROM acdoca
      WHERE rldnr = @lv_ledger AND rbukrs = @lv_bukrs AND gjahr = @lv_gjahr
        AND poper < @lv_from AND racct IN @lr_acct
      GROUP BY racct
      INTO TABLE @DATA(lt_open).
    SELECT racct, drcrk, SUM( hsl ) AS amount FROM acdoca
      WHERE rldnr = @lv_ledger AND rbukrs = @lv_bukrs AND gjahr = @lv_gjahr
        AND poper BETWEEN @lv_from AND @lv_to AND racct IN @lr_acct
      GROUP BY racct, drcrk
      INTO TABLE @DATA(lt_move).

    LOOP AT lt_open INTO DATA(ls_open).
      INSERT VALUE #( racct = ls_open-racct opening = ls_open-amount ) INTO TABLE lt_tb.
    ENDLOOP.
    LOOP AT lt_move INTO DATA(ls_move).
      READ TABLE lt_tb ASSIGNING FIELD-SYMBOL(<ls_tb>) WITH TABLE KEY racct = ls_move-racct.
      IF sy-subrc <> 0.
        INSERT VALUE #( racct = ls_move-racct ) INTO TABLE lt_tb ASSIGNING <ls_tb>.
      ENDIF.
      IF ls_move-drcrk = 'S'.
        <ls_tb>-debit = <ls_tb>-debit + ls_move-amount.
      ELSE.
        <ls_tb>-credit = <ls_tb>-credit + ls_move-amount.
      ENDIF.
    ENDLOOP.

    " Account names in the company code's chart of accounts.
    SELECT SINGLE ktopl FROM t001 WHERE bukrs = @lv_bukrs INTO @DATA(lv_ktopl).
    IF lt_tb IS NOT INITIAL.
      SELECT saknr, txt50 FROM skat
        FOR ALL ENTRIES IN @lt_tb
        WHERE spras = @sy-langu AND ktopl = @lv_ktopl AND saknr = @lt_tb-racct
        INTO TABLE @DATA(lt_txt).
    ENDIF.

    DATA(lv_no) = 0.
    LOOP AT lt_tb ASSIGNING <ls_tb>.
      <ls_tb>-closing = <ls_tb>-opening + <ls_tb>-debit + <ls_tb>-credit.
      IF <ls_tb>-opening = 0 AND <ls_tb>-debit = 0 AND <ls_tb>-credit = 0.
        CONTINUE.
      ENDIF.
      <ls_tb>-txt50 = VALUE #( lt_txt[ saknr = <ls_tb>-racct ]-txt50 OPTIONAL ).
      lv_no = lv_no + 1.
      APPEND to_row( iv_no = lv_no is_data = <ls_tb> ) TO rt_rows.
    ENDLOOP.
  ENDMETHOD.


  METHOD op_ageing.
    " Open items at KEY_DATE from ACDOCA (not cleared by then), per vendor (K)
    " or customer (D), bucketed by days past the net due date.
    TYPES: BEGIN OF ts_age,
             partner  TYPE lifnr,
             name1    TYPE name1_gp,
             total    TYPE decfloat34,
             not_due  TYPE decfloat34,
             d001_030 TYPE decfloat34,
             d031_060 TYPE decfloat34,
             d061_090 TYPE decfloat34,
             d091_180 TYPE decfloat34,
             over_180 TYPE decfloat34,
           END OF ts_age.
    DATA: lt_age  TYPE SORTED TABLE OF ts_age WITH UNIQUE KEY partner,
          lr_part TYPE RANGE OF lifnr.

    need_table( 'ACDOCA' ).
    DATA(lv_koart) = CONV koart( to_upper( is_p-account_type ) ).
    IF lv_koart <> 'K' AND lv_koart <> 'D'.
      fail( `AGEING needs ACCOUNT_TYPE 'K' (vendors) or 'D' (customers).` ).
    ENDIF.
    DATA(lv_bukrs)  = CONV bukrs( to_upper( is_p-company_code ) ).
    IF lv_bukrs IS INITIAL.
      fail( `AGEING needs COMPANY_CODE.` ).
    ENDIF.
    DATA(lv_key)    = CONV d( COND string( WHEN is_p-key_date IS INITIAL THEN sy-datum ELSE is_p-key_date ) ).
    DATA(lv_ledger) = CONV fins_ledger( COND string( WHEN is_p-ledger IS INITIAL THEN `0L`
                                                     ELSE to_upper( is_p-ledger ) ) ).
    IF is_p-partner IS NOT INITIAL.
      lr_part = VALUE #( ( sign = 'I' option = 'EQ' low = |{ is_p-partner ALPHA = IN WIDTH = 10 }| ) ).
    ENDIF.

    IF lv_koart = 'K'.
      SELECT lifnr AS partner, netdt, SUM( hsl ) AS amount FROM acdoca
        WHERE rldnr = @lv_ledger AND rbukrs = @lv_bukrs AND koart = 'K'
          AND budat <= @lv_key AND ( augbl = '' OR augdt > @lv_key )
          AND lifnr IN @lr_part
        GROUP BY lifnr, netdt
        INTO TABLE @DATA(lt_items).
    ELSE.
      SELECT kunnr AS partner, netdt, SUM( hsl ) AS amount FROM acdoca
        WHERE rldnr = @lv_ledger AND rbukrs = @lv_bukrs AND koart = 'D'
          AND budat <= @lv_key AND ( augbl = '' OR augdt > @lv_key )
          AND kunnr IN @lr_part
        GROUP BY kunnr, netdt
        INTO TABLE @lt_items.
    ENDIF.

    LOOP AT lt_items INTO DATA(ls_item).
      IF ls_item-partner IS INITIAL.
        CONTINUE.   " no vendor / customer on the line (e.g. technical lines)
      ENDIF.
      READ TABLE lt_age ASSIGNING FIELD-SYMBOL(<ls_age>) WITH TABLE KEY partner = ls_item-partner.
      IF sy-subrc <> 0.
        INSERT VALUE #( partner = ls_item-partner ) INTO TABLE lt_age ASSIGNING <ls_age>.
      ENDIF.
      DATA(lv_days) = COND i( WHEN ls_item-netdt IS INITIAL THEN 0 ELSE lv_key - ls_item-netdt ).
      <ls_age>-total = <ls_age>-total + ls_item-amount.
      IF lv_days <= 0.
        <ls_age>-not_due = <ls_age>-not_due + ls_item-amount.
      ELSEIF lv_days <= 30.
        <ls_age>-d001_030 = <ls_age>-d001_030 + ls_item-amount.
      ELSEIF lv_days <= 60.
        <ls_age>-d031_060 = <ls_age>-d031_060 + ls_item-amount.
      ELSEIF lv_days <= 90.
        <ls_age>-d061_090 = <ls_age>-d061_090 + ls_item-amount.
      ELSEIF lv_days <= 180.
        <ls_age>-d091_180 = <ls_age>-d091_180 + ls_item-amount.
      ELSE.
        <ls_age>-over_180 = <ls_age>-over_180 + ls_item-amount.
      ENDIF.
    ENDLOOP.

    IF lt_age IS NOT INITIAL.
      IF lv_koart = 'K'.
        SELECT lifnr AS partner, name1 FROM lfa1 FOR ALL ENTRIES IN @lt_age
          WHERE lifnr = @lt_age-partner INTO TABLE @DATA(lt_names).
      ELSE.
        SELECT kunnr AS partner, name1 FROM kna1 FOR ALL ENTRIES IN @lt_age
          WHERE kunnr = @lt_age-partner INTO TABLE @lt_names.
      ENDIF.
    ENDIF.

    DATA(lv_no) = 0.
    LOOP AT lt_age ASSIGNING <ls_age>.
      IF <ls_age>-total = 0.
        CONTINUE.
      ENDIF.
      <ls_age>-name1 = VALUE #( lt_names[ partner = <ls_age>-partner ]-name1 OPTIONAL ).
      lv_no = lv_no + 1.
      IF lv_no > is_p-max_rows.
        EXIT.
      ENDIF.
      APPEND to_row( iv_no = lv_no is_data = <ls_age> ) TO rt_rows.
    ENDLOOP.
  ENDMETHOD.


  METHOD op_grir.
    " Per PO item: goods receipts (VGABE 1) vs invoice receipts (VGABE 2) from
    " EKBE, in company-code currency, up to KEY_DATE. Only items that differ.
    TYPES: BEGIN OF ts_gi,
             ebeln      TYPE ebeln,
             ebelp      TYPE ebelp,
             lifnr      TYPE lifnr,
             gr_qty     TYPE decfloat34,
             ir_qty     TYPE decfloat34,
             gr_value   TYPE decfloat34,
             ir_value   TYPE decfloat34,
             diff_value TYPE decfloat34,
           END OF ts_gi.
    DATA: lt_gi   TYPE SORTED TABLE OF ts_gi WITH UNIQUE KEY ebeln ebelp,
          lr_lif  TYPE RANGE OF lifnr,
          lr_po   TYPE RANGE OF ebeln.

    need_table( 'EKBE' ).
    need_table( 'EKKO' ).
    DATA(lv_bukrs) = CONV bukrs( to_upper( is_p-company_code ) ).
    IF lv_bukrs IS INITIAL.
      fail( `GRIR needs COMPANY_CODE.` ).
    ENDIF.
    DATA(lv_key) = CONV d( COND string( WHEN is_p-key_date IS INITIAL THEN sy-datum ELSE is_p-key_date ) ).
    IF is_p-vendor IS NOT INITIAL.
      lr_lif = VALUE #( ( sign = 'I' option = 'EQ' low = |{ is_p-vendor ALPHA = IN WIDTH = 10 }| ) ).
    ENDIF.
    IF is_p-po IS NOT INITIAL.
      lr_po = VALUE #( ( sign = 'I' option = 'EQ' low = |{ is_p-po ALPHA = IN WIDTH = 10 }| ) ).
    ENDIF.

    SELECT b~ebeln, b~ebelp, k~lifnr, b~vgabe, b~shkzg,
           SUM( b~dmbtr ) AS amount, SUM( b~menge ) AS qty
      FROM ekbe AS b INNER JOIN ekko AS k ON k~ebeln = b~ebeln
      WHERE k~bukrs = @lv_bukrs AND b~vgabe IN ('1', '2') AND b~budat <= @lv_key
        AND k~lifnr IN @lr_lif AND b~ebeln IN @lr_po
      GROUP BY b~ebeln, b~ebelp, k~lifnr, b~vgabe, b~shkzg
      INTO TABLE @DATA(lt_hist).

    LOOP AT lt_hist INTO DATA(ls_h).
      READ TABLE lt_gi ASSIGNING FIELD-SYMBOL(<ls_gi>)
        WITH TABLE KEY ebeln = ls_h-ebeln ebelp = ls_h-ebelp.
      IF sy-subrc <> 0.
        INSERT VALUE #( ebeln = ls_h-ebeln ebelp = ls_h-ebelp lifnr = ls_h-lifnr )
          INTO TABLE lt_gi ASSIGNING <ls_gi>.
      ENDIF.
      DATA(lv_sign) = COND decfloat34( WHEN ls_h-shkzg = 'H' THEN -1 ELSE 1 ).
      IF ls_h-vgabe = '1'.
        <ls_gi>-gr_qty   = <ls_gi>-gr_qty   + lv_sign * ls_h-qty.
        <ls_gi>-gr_value = <ls_gi>-gr_value + lv_sign * ls_h-amount.
      ELSE.
        <ls_gi>-ir_qty   = <ls_gi>-ir_qty   + lv_sign * ls_h-qty.
        <ls_gi>-ir_value = <ls_gi>-ir_value + lv_sign * ls_h-amount.
      ENDIF.
    ENDLOOP.

    DATA(lv_no) = 0.
    LOOP AT lt_gi ASSIGNING <ls_gi>.
      <ls_gi>-diff_value = <ls_gi>-gr_value - <ls_gi>-ir_value.
      IF <ls_gi>-diff_value = 0 AND <ls_gi>-gr_qty = <ls_gi>-ir_qty.
        CONTINUE.
      ENDIF.
      lv_no = lv_no + 1.
      IF lv_no > is_p-max_rows.
        EXIT.
      ENDIF.
      APPEND to_row( iv_no = lv_no is_data = <ls_gi> ) TO rt_rows.
    ENDLOOP.
  ENDMETHOD.


  METHOD resolve_report.
    " A report program, or a transaction code that starts one (TSTC).
    SELECT SINGLE name FROM trdir WHERE name = @iv_name AND subc = '1' INTO @rv_prog.
    IF sy-subrc = 0.
      RETURN.
    ENDIF.
    SELECT SINGLE pgmna FROM tstc WHERE tcode = @iv_name INTO @DATA(lv_pgm).
    IF sy-subrc = 0 AND lv_pgm IS NOT INITIAL.
      SELECT SINGLE name FROM trdir WHERE name = @lv_pgm AND subc = '1' INTO @rv_prog.
      IF sy-subrc = 0.
        RETURN.
      ENDIF.
      fail( |Transaction { iv_name } starts { lv_pgm }, which is not an executable report.| ).
    ENDIF.
    fail( |{ iv_name } is neither an executable report nor a report transaction.| ).
  ENDMETHOD.


  METHOD op_reports.
    TYPES: BEGIN OF ts_info,
             kind  TYPE string,
             name  TYPE string,
             type  TYPE string,
             text  TYPE string,
           END OF ts_info,
           BEGIN OF ts_col,
             field  TYPE string,
             text   TYPE string,
             hidden TYPE abap_bool,
           END OF ts_col,
           tt_col TYPE STANDARD TABLE OF ts_col WITH EMPTY KEY,
           BEGIN OF ts_cols,
             columns TYPE tt_col,
           END OF ts_cols,
           ty_line TYPE c LENGTH 1024,
           BEGIN OF ts_line,
             line TYPE string,
           END OF ts_line.
    DATA: lt_info  TYPE STANDARD TABLE OF rsel_info,
          lt_text  TYPE STANDARD TABLE OF textpool,
          lt_sel   TYPE rsparams_tt,
          lr_data  TYPE REF TO data,
          lt_list  TYPE STANDARD TABLE OF abaplist,
          lt_ascii TYPE STANDARD TABLE OF ty_line.
    FIELD-SYMBOLS <lt_out> TYPE ANY TABLE.

    " Program name as a fixed-length field: READ TEXTPOOL and SUBMIT need one.
    DATA(lv_report) = CONV syrepid( to_upper( condense( is_p-report ) ) ).

    IF iv_op = 'LIST_REPORTS'.
      " REPORT is a pattern here (ZFI*, *AGEING*); default: all Z reports.
      DATA(lv_pat) = replace( val = COND string( WHEN lv_report IS INITIAL THEN `Z*` ELSE lv_report )
                              sub = `*` with = `%` occ = 0 ).
      DATA(lv_max) = is_p-max_rows.
      SELECT d~name, t~text FROM trdir AS d
        LEFT OUTER JOIN trdirt AS t ON t~name = d~name AND t~sprsl = @sy-langu
        WHERE d~name LIKE @lv_pat AND d~subc = '1'
        ORDER BY d~name
        INTO TABLE @DATA(lt_prog) UP TO @lv_max ROWS.
      LOOP AT lt_prog INTO DATA(ls_prog).
        APPEND to_row( iv_no = lines( rt_rows ) + 1
                       is_data = VALUE ts_info( kind = `REPORT` name = ls_prog-name text = ls_prog-text ) )
          TO rt_rows.
      ENDLOOP.
      " Transaction codes that start a report (TYPE = the program behind it).
      SELECT c~tcode, c~pgmna, t~ttext FROM tstc AS c
        LEFT OUTER JOIN tstct AS t ON t~tcode = c~tcode AND t~sprsl = @sy-langu
        WHERE c~tcode LIKE @lv_pat AND c~pgmna <> ''
        ORDER BY c~tcode
        INTO TABLE @DATA(lt_tcode) UP TO @lv_max ROWS.
      LOOP AT lt_tcode INTO DATA(ls_tcode).
        APPEND to_row( iv_no = lines( rt_rows ) + 1
                       is_data = VALUE ts_info( kind = `TCODE` name = ls_tcode-tcode
                                                type = ls_tcode-pgmna text = ls_tcode-ttext ) )
          TO rt_rows.
      ENDLOOP.
      RETURN.
    ENDIF.

    IF lv_report IS INITIAL.
      fail( `REPORT is required.` ).
    ENDIF.
    lv_report = resolve_report( lv_report ).   " transaction code -> its program

    CALL FUNCTION 'RS_REPORTSELECTIONS_INFO'
      EXPORTING
        report              = lv_report
      TABLES
        field_info          = lt_info
      EXCEPTIONS
        no_selections       = 1
        report_not_existent = 2
        subroutine_pool     = 3
        OTHERS              = 4.
    READ TEXTPOOL lv_report INTO lt_text LANGUAGE sy-langu.

    IF iv_op = 'REPORT_INFO'.
      LOOP AT lt_info INTO DATA(ls_info).
        " Selection texts: the first 8 characters of ENTRY are a reserved prefix.
        DATA(lv_seltext) = VALUE string( lt_text[ id = 'S' key = ls_info-name ]-entry OPTIONAL ).
        lv_seltext = COND #( WHEN strlen( lv_seltext ) > 8 THEN condense( substring( val = lv_seltext off = 8 ) )
                             ELSE condense( lv_seltext ) ).
        APPEND to_row( iv_no = lines( rt_rows ) + 1
                       is_data = VALUE ts_info( kind = COND #( WHEN ls_info-kind = 'S' THEN `SELECT-OPTION`
                                                               ELSE `PARAMETER` )
                                                name = ls_info-name
                                                type = ls_info-dbfield
                                                text = lv_seltext ) )
          TO rt_rows.
      ENDLOOP.
      SELECT v~variant, t~vtext FROM varid AS v
        LEFT OUTER JOIN varit AS t ON t~report = v~report AND t~variant = v~variant AND t~langu = @sy-langu
        WHERE v~report = @lv_report
        INTO TABLE @DATA(lt_var).
      LOOP AT lt_var INTO DATA(ls_var).
        APPEND to_row( iv_no = lines( rt_rows ) + 1
                       is_data = VALUE ts_info( kind = `VARIANT` name = ls_var-variant text = ls_var-vtext ) )
          TO rt_rows.
      ENDLOOP.
      RETURN.
    ENDIF.

    " RUN_REPORT: selections arrive without KIND; take it from the selection screen.
    LOOP AT is_p-selections INTO DATA(ls_s).
      ls_s-selname = to_upper( ls_s-selname ).
      READ TABLE lt_info INTO ls_info WITH KEY name = ls_s-selname.
      IF sy-subrc <> 0.
        fail( |{ lv_report } has no selection field { ls_s-selname }.| ).
      ENDIF.
      ls_s-kind = ls_info-kind.
      IF ls_s-sign IS INITIAL.
        ls_s-sign = 'I'.
      ENDIF.
      IF ls_s-option IS INITIAL.
        ls_s-option = COND #( WHEN ls_s-high IS NOT INITIAL THEN 'BT'
                              WHEN ls_s-low CA '*+' THEN 'CP' ELSE 'EQ' ).
      ENDIF.
      APPEND ls_s TO lt_sel.
    ENDLOOP.
    DATA(lv_variant) = CONV raldb_vari( to_upper( condense( is_p-variant ) ) ).

    cl_salv_bs_runtime_info=>set( display  = abap_false
                                  metadata = abap_true
                                  data     = abap_true ).
    IF lv_variant IS INITIAL.
      SUBMIT (lv_report) WITH SELECTION-TABLE lt_sel
        EXPORTING LIST TO MEMORY AND RETURN.
    ELSE.
      SUBMIT (lv_report) USING SELECTION-SET lv_variant WITH SELECTION-TABLE lt_sel
        EXPORTING LIST TO MEMORY AND RETURN.
    ENDIF.

    TRY.
        cl_salv_bs_runtime_info=>get_data_ref( IMPORTING r_data = lr_data ).
        DATA(ls_meta) = cl_salv_bs_runtime_info=>get_metadata( ).
      CATCH cx_salv_bs_sc_runtime_info.
        CLEAR lr_data.
    ENDTRY.
    cl_salv_bs_runtime_info=>clear_all( ).

    IF lr_data IS BOUND.
      " ALV report: row 0 carries the column headings, then the data rows.
      DATA(ls_cols) = VALUE ts_cols( ).
      LOOP AT ls_meta-t_fcat INTO DATA(ls_fcat).
        APPEND VALUE #( field  = ls_fcat-fieldname
                        text   = COND #( WHEN ls_fcat-coltext IS NOT INITIAL THEN ls_fcat-coltext
                                         WHEN ls_fcat-scrtext_m IS NOT INITIAL THEN ls_fcat-scrtext_m
                                         ELSE ls_fcat-reptext )
                        hidden = xsdbool( ls_fcat-no_out = abap_true OR ls_fcat-tech = abap_true ) )
          TO ls_cols-columns.
      ENDLOOP.
      APPEND to_row( iv_no = 0 is_data = ls_cols ) TO rt_rows.
      ASSIGN lr_data->* TO <lt_out>.
      LOOP AT <lt_out> ASSIGNING FIELD-SYMBOL(<ls_out>).
        IF lines( rt_rows ) > is_p-max_rows.
          EXIT.
        ENDIF.
        APPEND to_row( iv_no = lines( rt_rows ) is_data = <ls_out> ) TO rt_rows.
      ENDLOOP.
      RETURN.
    ENDIF.

    " Classic list report: the printed list as text lines.
    CALL FUNCTION 'LIST_FROM_MEMORY'
      TABLES
        listobject = lt_list
      EXCEPTIONS
        not_found  = 1
        OTHERS     = 2.
    IF sy-subrc = 0.
      CALL FUNCTION 'LIST_TO_ASCI'
        TABLES
          listasci   = lt_ascii
          listobject = lt_list
        EXCEPTIONS
          OTHERS     = 1.
      CALL FUNCTION 'LIST_FREE_MEMORY'
        TABLES
          listobject = lt_list.
    ENDIF.
    LOOP AT lt_ascii INTO DATA(lv_line).
      IF lines( rt_rows ) >= is_p-max_rows.
        EXIT.
      ENDIF.
      APPEND to_row( iv_no = lines( rt_rows ) + 1
                     is_data = VALUE ts_line( line = CONV string( lv_line ) ) )   " keeps column spacing
        TO rt_rows.
    ENDLOOP.
    IF rt_rows IS INITIAL.
      fail( |{ lv_report } produced no output for these selections.| ).
    ENDIF.
  ENDMETHOD.


  METHOD op_attachments.
    TYPES: BEGIN OF ts_att,
             doc_id      TYPE string,
             relation    TYPE string,
             file_name   TYPE string,
             extension   TYPE string,
             size_bytes  TYPE i,
             description TYPE string,
             created_by  TYPE string,
             created_on  TYPE string,
             content     TYPE string,
           END OF ts_att.
    DATA lt_rel TYPE STANDARD TABLE OF srgbtbrel WITH EMPTY KEY.

    DATA(lv_type)  = to_upper( condense( is_p-object_type ) ).
    DATA(lv_key)   = condense( is_p-object_key ).
    DATA(lv_docid) = condense( is_p-doc_id ).
    IF lv_type IS INITIAL OR lv_key IS INITIAL.
      fail( `OBJECT_TYPE and OBJECT_KEY are required (e.g. BUS2081 / 51000676202026).` ).
    ENDIF.
    IF iv_with_content = abap_true AND lv_docid IS INITIAL.
      fail( `DOC_ID is required; take it from LIST_ATTACHMENTS.` ).
    ENDIF.
    need_table( 'SOOD' ).

    SELECT * FROM srgbtbrel
      WHERE typeid_a = @lv_type
        AND instid_a = @lv_key
        AND catid_a  = 'BO'
        AND reltype IN ('ATTA', 'NOTE', 'URL')
      INTO TABLE @lt_rel.

    LOOP AT lt_rel INTO DATA(ls_rel).
      DATA(lv_id) = CONV string( ls_rel-instid_b ).
      IF iv_with_content = abap_true AND lv_id <> lv_docid.
        CONTINUE.
      ENDIF.
      IF strlen( lv_id ) < 34.
        CONTINUE.   " not a SAPoffice document id
      ENDIF.

      " Document id = folder (17 chars) + object (OBJTP 3, OBJYR 2, OBJNO 12).
      DATA(lv_objtp) = CONV sood-objtp( substring( val = lv_id off = 17 len = 3 ) ).
      DATA(lv_objyr) = CONV sood-objyr( substring( val = lv_id off = 20 len = 2 ) ).
      DATA(lv_objno) = CONV sood-objno( substring( val = lv_id off = 22 len = 12 ) ).
      SELECT SINGLE objdes, file_ext, objlen, cronam, crdat, crtim
        FROM sood
        WHERE objtp = @lv_objtp
          AND objyr = @lv_objyr
          AND objno = @lv_objno
        INTO @DATA(ls_sood).
      IF sy-subrc <> 0.
        CONTINUE.
      ENDIF.

      DATA(lv_ext)  = to_lower( condense( CONV string( ls_sood-file_ext ) ) ).
      DATA(lv_desc) = condense( CONV string( ls_sood-objdes ) ).
      DATA(ls_att) = VALUE ts_att(
        doc_id      = lv_id
        relation    = ls_rel-reltype
        file_name   = COND #( WHEN lv_ext IS INITIAL THEN lv_desc ELSE |{ lv_desc }.{ lv_ext }| )
        extension   = lv_ext
        size_bytes  = ls_sood-objlen
        description = lv_desc
        created_by  = ls_sood-cronam
        created_on  = |{ ls_sood-crdat }{ ls_sood-crtim }| ).

      IF iv_with_content = abap_true.
        IF ls_sood-objlen > c_max_file.
          fail( |Attachment is { ls_sood-objlen } bytes; the limit is { c_max_file }.| ).
        ENDIF.
        attachment_content( EXPORTING iv_doc_id    = lv_id
                                      iv_size      = CONV i( ls_sood-objlen )
                            IMPORTING ev_file_name = DATA(lv_name)
                                      ev_base64    = ls_att-content ).
        IF lv_name IS NOT INITIAL.
          ls_att-file_name = lv_name.
        ENDIF.
      ENDIF.
      APPEND to_row( iv_no = lines( rt_rows ) + 1 is_data = ls_att ) TO rt_rows.
    ENDLOOP.

    IF iv_with_content = abap_true AND rt_rows IS INITIAL.
      fail( |Document { lv_docid } is not attached to { lv_type } { lv_key }.| ).
    ENDIF.
  ENDMETHOD.


  METHOD need_table.
    AUTHORITY-CHECK OBJECT 'S_TABU_NAM'
      ID 'ACTVT' FIELD '03'
      ID 'TABLE' FIELD iv_table.
    IF sy-subrc <> 0.
      fail( |No authorization to display table { iv_table } (S_TABU_NAM, activity 03).| ).
    ENDIF.
  ENDMETHOD.


  METHOD to_row.
    rs_row-row_no = iv_no.
    rs_row-data   = /ui2/cl_json=>serialize( data           = is_data
                                             numc_as_string = abap_true ).
  ENDMETHOD.


  METHOD field_list.
    DATA(lv_text) = to_upper( translate( val = iv_text from = ` ` to = `,` ) ).
    SPLIT lv_text AT ',' INTO TABLE rt_list.
    DELETE rt_list WHERE table_line IS INITIAL.
  ENDMETHOD.


  METHOD get_table_fields.
    DATA lv_table TYPE tabname.

    lv_table = to_upper( condense( param( it_parameter = it_parameter iv_name = `TableName` ) ) ).
    IF lv_table IS INITIAL.
      fail( `TableName is required.` ).
    ENDIF.

    DATA(lt_dfies) = ddic_fields( lv_table ).
    LOOP AT lt_dfies INTO DATA(ls_f).
      APPEND VALUE #( field_name   = ls_f-fieldname
                      position     = ls_f-position
                      key_flag     = ls_f-keyflag
                      data_type    = ls_f-datatype
                      length       = ls_f-leng
                      decimals     = ls_f-decimals
                      check_table  = ls_f-checktable
                      data_element = ls_f-rollname
                      description  = COND #( WHEN ls_f-fieldtext IS NOT INITIAL
                                             THEN ls_f-fieldtext
                                             ELSE ls_f-scrtext_l ) )
        TO rt_fields.
    ENDLOOP.
  ENDMETHOD.


  METHOD attachment_content.
    DATA: ls_doc     TYPE sofolenti1,
          lt_header  TYPE STANDARD TABLE OF solisti1,
          lt_text    TYPE STANDARD TABLE OF solisti1,
          lt_hex     TYPE solix_tab,
          lv_docid   TYPE sofolenti1-doc_id,
          lv_xstring TYPE xstring.

    lv_docid = iv_doc_id.
    CALL FUNCTION 'SO_DOCUMENT_READ_API1'
      EXPORTING
        document_id                = lv_docid
      IMPORTING
        document_data              = ls_doc
      TABLES
        object_header              = lt_header
        object_content             = lt_text
        contents_hex               = lt_hex
      EXCEPTIONS
        document_id_not_exist      = 1
        operation_no_authorization = 2
        x_error                    = 3
        OTHERS                     = 4.
    IF sy-subrc = 2.
      fail( `No SAPoffice authorization for this attachment.` ).
    ELSEIF sy-subrc <> 0.
      fail( |Could not read attachment { iv_doc_id } (SO_DOCUMENT_READ_API1 rc { sy-subrc }).| ).
    ENDIF.

    " Original file name, when the upload recorded one.
    LOOP AT lt_header INTO DATA(ls_head).
      IF ls_head-line CS '&SO_FILENAME='.
        ev_file_name = condense( substring_after( val = ls_head-line sub = '&SO_FILENAME=' ) ).
      ENDIF.
    ENDLOOP.

    IF lt_hex IS NOT INITIAL.
      lv_xstring = cl_bcs_convert=>solix_to_xstring( it_solix = lt_hex
                                                     iv_size  = iv_size ).
    ELSE.
      " Notes and text documents come back as text lines.
      DATA(lv_text) = concat_lines_of( table = lt_text sep = cl_abap_char_utilities=>cr_lf ).
      lv_xstring = cl_abap_codepage=>convert_to( source = lv_text codepage = `UTF-8` ).
    ENDIF.
    ev_base64 = cl_http_utility=>encode_x_base64( lv_xstring ).
  ENDMETHOD.


  METHOD param.
    LOOP AT it_parameter INTO DATA(ls_param).
      IF to_upper( ls_param-name ) = to_upper( iv_name ).
        rv_value = ls_param-value.
        RETURN.
      ENDIF.
    ENDLOOP.
  ENDMETHOD.


  METHOD int_param.
    DATA(lv_text) = condense( param( it_parameter = it_parameter iv_name = iv_name ) ).
    IF lv_text IS NOT INITIAL AND lv_text CO '0123456789'.
      rv_value = lv_text.
    ELSE.
      rv_value = iv_default.
    ENDIF.
  ENDMETHOD.


  METHOD check_table.
    " Defence in depth: password hashes and connection secrets stay closed
    " even to users whose S_TABU_NAM would allow them.
    DATA lt_blocked TYPE RANGE OF tabname.
    lt_blocked = VALUE #( sign = 'I' option = 'EQ'
                          ( low = 'USR02' )  ( low = 'USH02' )     ( low = 'USRPWDHISTORY' )
                          ( low = 'RFCDES' ) ( low = 'SSF_PSE_D' ) ( low = 'RSECTAB' )
                          ( low = 'SECSTORE' ) ).
    IF iv_table IN lt_blocked.
      fail( |Table { iv_table } is blocked in ZCFG_READ_SRV (credentials / secrets).| ).
    ENDIF.

    SELECT SINGLE tabclass FROM dd02l
      WHERE tabname  = @iv_table
        AND as4local = 'A'
        AND as4vers  = '0000'
      INTO @DATA(lv_tabclass).
    IF sy-subrc <> 0.
      fail( |Table { iv_table } does not exist or is not active.| ).
    ENDIF.

    CASE lv_tabclass.
      WHEN 'TRANSP' OR 'POOL' OR 'CLUSTER'.
        " readable
      WHEN 'VIEW'.
        SELECT SINGLE viewclass FROM dd25l
          WHERE viewname = @iv_table
            AND as4local = 'A'
            AND as4vers  = '0000'
          INTO @DATA(lv_viewclass).
        IF lv_viewclass <> 'D'.
          fail( |{ iv_table } is not a database view and cannot be read.| ).
        ENDIF.
      WHEN OTHERS.
        fail( |{ iv_table } is a structure, not a table (class { lv_tabclass }).| ).
    ENDCASE.

    AUTHORITY-CHECK OBJECT 'S_TABU_NAM'
      ID 'ACTVT' FIELD '03'
      ID 'TABLE' FIELD iv_table.
    IF sy-subrc <> 0.
      fail( |No authorization to display table { iv_table } (S_TABU_NAM, activity 03).| ).
    ENDIF.
  ENDMETHOD.


  METHOD check_where.
    IF iv_where IS INITIAL.
      RETURN.
    ENDIF.
    IF strlen( iv_where ) > c_max_where.
      fail( |WHERE clause is longer than { c_max_where } characters.| ).
    ENDIF.

    " Blank out quoted literals ('...' with '' as an escaped quote) so their
    " contents can neither trigger nor hide a match below.
    DATA(lv_code) = to_upper( replace( val  = iv_where
                                       pcre = `'(?:[^']|'')*'`
                                       with = `''`
                                       occ  = 0 ) ).

    IF lv_code CA '`@"'.
      fail( `WHERE may not contain backquotes, host variables (@) or comments.` ).
    ENDIF.
    IF count( val = lv_code sub = `'` ) MOD 2 <> 0.
      fail( `WHERE has an unbalanced quote.` ).
    ENDIF.
    IF find( val = lv_code pcre = `\b(SELECT|FROM|JOIN|UNION|EXISTS|INTO)\b` ) >= 0.
      fail( `Sub-queries and SELECT/FROM/JOIN/UNION/EXISTS are not allowed in WHERE.` ).
    ENDIF.
  ENDMETHOD.


  METHOD ddic_fields.
    DATA: lo_type   TYPE REF TO cl_abap_typedescr,
          lo_struct TYPE REF TO cl_abap_structdescr.

    cl_abap_typedescr=>describe_by_name(
      EXPORTING  p_name         = iv_table
      RECEIVING  p_descr_ref    = lo_type
      EXCEPTIONS type_not_found = 1
                 OTHERS         = 2 ).
    IF sy-subrc <> 0.
      fail( |Table { iv_table } not found in the ABAP Dictionary.| ).
    ENDIF.
    IF lo_type->kind <> cl_abap_typedescr=>kind_struct.
      fail( |{ iv_table } is not a table or structure.| ).
    ENDIF.
    lo_struct ?= lo_type.

    lo_struct->get_ddic_field_list(
      EXPORTING  p_langu      = sy-langu
      RECEIVING  p_field_list = rt_dfies
      EXCEPTIONS not_found    = 1
                 no_ddic_type = 2
                 OTHERS       = 3 ).
    IF sy-subrc <> 0.
      fail( |No dictionary field list for { iv_table }.| ).
    ENDIF.
    DELETE rt_dfies WHERE fieldname CP '.*'.   " .INCLUDE / .APPEND markers
  ENDMETHOD.


  METHOD fail.
    RAISE EXCEPTION TYPE /iwbep/cx_mgw_busi_exception
      EXPORTING
        textid            = /iwbep/cx_mgw_busi_exception=>business_error_unlimited
        message_unlimited = CONV string( iv_text ).
  ENDMETHOD.

ENDCLASS.
